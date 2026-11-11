"""Варіант `jsonb`: PostgreSQL, таблиця bench.trip_docs.

Вирази в WHERE і ORDER BY мають збігатися з виразами індексів у sql/02_indexes.sql, інакше індекс не використається.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .. import codec
from ..db import pg_connect
from .base import EXPLAINABLE, ServerPlan
from .sql import COPY_MIN, D2, money, pg_explain, utc, year_bounds

_z = codec._iso      # datetime у рядок "YYYY-MM-DDTHH:MM:SSZ"

S1_SQL = "SELECT doc::text FROM bench.trip_docs WHERE trip_number = %s"

S2_SQL = """
SELECT trip_number, doc->>'planned_start', doc->>'status', doc->'route'->>'from', doc->'route'->>'to',
       doc->'customer'->>'name', (doc->'totals'->>'freight_price')::numeric
  FROM bench.trip_docs
 WHERE (doc->'driver'->>'id')::int = %s AND doc->>'planned_start' >= %s AND doc->>'planned_start' < %s
 ORDER BY doc->>'planned_start' DESC, trip_number DESC
OFFSET %s LIMIT %s
"""

S3_SQL = """
UPDATE bench.trip_docs SET doc = jsonb_set(doc, '{events}', (doc->'events') || %s::jsonb)
 WHERE trip_number = %s
"""
# fuel: події і підсумки оновлюються одним UPDATE
S3_FUEL_SQL = """
UPDATE bench.trip_docs SET doc = jsonb_set(jsonb_set(jsonb_set(doc,
           '{events}', (doc->'events') || %s::jsonb),
           '{totals,fuel_l}',    to_jsonb((doc->'totals'->>'fuel_l')::numeric + %s::numeric)),
           '{totals,fuel_cost}', to_jsonb((doc->'totals'->>'fuel_cost')::numeric + %s::numeric))
 WHERE trip_number = %s
"""

S4_SQL = "INSERT INTO bench.trip_docs VALUES (%s, %s::jsonb)"

S5A_SQL = """
SELECT (doc->'customer'->>'id')::int, substring(doc->>'planned_start', 6, 2)::int, count(*),
       sum((doc->'totals'->>'freight_price')::numeric)
  FROM bench.trip_docs
 WHERE doc->>'planned_start' >= %s AND doc->>'planned_start' < %s AND doc->>'status' = 'completed'
 GROUP BY 1, 2 ORDER BY 1, 2
"""

S5B_SQL = """
SELECT (d.doc->'vehicle'->>'id')::int, count(*), sum((e->>'liters')::numeric),
       sum(round((e->>'liters')::numeric * (e->>'unit_price')::numeric, 2))
  FROM bench.trip_docs d, jsonb_array_elements(d.doc->'events') e
 WHERE d.doc->>'planned_start' >= %s AND d.doc->>'planned_start' < %s
   AND e->>'type' = 'fuel' AND e->>'at' >= %s AND e->>'at' < %s
 GROUP BY 1 ORDER BY 1
"""

S6_ALL_SQL = """
UPDATE bench.trip_docs SET doc = jsonb_set(doc, '{customer,name}', to_jsonb(%s::text))
 WHERE (doc->'customer'->>'id')::int = %s
"""
S6_OPEN_SQL = S6_ALL_SQL + " AND doc->>'status' IN ('planned', 'in_progress')"

S7_SQL = """
SELECT trip_number, doc->>'planned_start'
  FROM bench.trip_docs
 WHERE doc->'cargo' @> %s::jsonb AND doc->>'planned_start' >= %s AND doc->>'planned_start' < %s
 ORDER BY doc->>'planned_start', trip_number
"""

DELETE_SQL = "DELETE FROM bench.trip_docs WHERE trip_number = ANY(%s)"


def _s5b_params(year: int) -> tuple[str, str, str, str]:
    a, b = year_bounds(year)
    # документи, події яких можуть потрапити в рік. Генератор ставить події в межах від planned_start - 2 год
    # до planned_start + 7 днів, тому planned_start береться з [рік - 7 днів, рік + 1 + 2 год).
    return _z(a - timedelta(days=7)), _z(b + timedelta(hours=2)), _z(a), _z(b)


class JsonbRepository:
    name = "jsonb"

    def __init__(self) -> None:
        self.conn = pg_connect(autocommit=True)

    def close(self) -> None:
        self.conn.close()

    # ---- S1
    def get_trip(self, trip_number: str) -> dict | None:
        row = self.conn.execute(S1_SQL, (trip_number,)).fetchone()
        return None if row is None else codec.from_jsonb(row[0])

    # ---- S2
    def driver_trips(self, driver_id, date_from, date_to, page, page_size=20) -> list[tuple]:
        rows = self.conn.execute(
            S2_SQL, (driver_id, _z(date_from), _z(date_to), page * page_size, page_size)).fetchall()
        return [(tn, codec._parse_dt(ps), st, f, t, cn, fp.quantize(D2)) for tn, ps, st, f, t, cn, fp in rows]

    # ---- S3
    def append_event(self, trip_number: str, event: dict) -> None:
        ev = json.dumps(event, default=codec._default, ensure_ascii=False)
        if event["type"] == "fuel":
            liters = event["liters"]
            cur = self.conn.execute(S3_FUEL_SQL, (ev, liters, money(liters, event["unit_price"]), trip_number))
        else:
            cur = self.conn.execute(S3_SQL, (ev, trip_number))
        if cur.rowcount == 0:
            raise LookupError(f"Рейс {trip_number} не знайдено")

    # ---- S4
    def insert_trips(self, docs: list[dict]) -> None:
        """Як у SQL: малі батчі через executemany, від COPY_MIN документів через COPY."""
        (self.insert_trips_copy if len(docs) >= COPY_MIN else self.insert_trips_executemany)(docs)

    def insert_trips_copy(self, docs: list[dict]) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            with cur.copy("COPY bench.trip_docs (trip_number, doc) FROM STDIN") as cp:
                for d in docs:
                    cp.write_row((d["trip_number"], codec.to_jsonb(d)))

    def insert_trips_executemany(self, docs: list[dict]) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(S4_SQL, [(d["trip_number"], codec.to_jsonb(d)) for d in docs])

    # ---- S5
    def revenue_by_customer_month(self, year: int) -> list[tuple]:
        a, b = year_bounds(year)
        rows = self.conn.execute(S5A_SQL, (_z(a), _z(b))).fetchall()
        return [(c, m, n, r.quantize(D2)) for c, m, n, r in rows]

    def fuel_by_vehicle(self, year: int) -> list[tuple]:
        rows = self.conn.execute(S5B_SQL, _s5b_params(year)).fetchall()
        return [(v, n, l.quantize(D2), c.quantize(D2)) for v, n, l, c in rows]

    # ---- S6
    def rename_customer(self, customer_id: int, new_name: str, scope: str = "all") -> int:
        sql = S6_OPEN_SQL if scope == "open" else S6_ALL_SQL
        return self.conn.execute(sql, (new_name, customer_id)).rowcount

    # ---- S7
    def adr_trips(self, adr_class: int, date_from: datetime, date_to: datetime) -> list[tuple]:
        rows = self.conn.execute(
            S7_SQL, (json.dumps([{"adr_class": adr_class}]), _z(date_from), _z(date_to))).fetchall()
        return [(tn, codec._parse_dt(ps)) for tn, ps in rows]

    # ---- explain
    def explain(self, op: str, **kw: Any) -> ServerPlan:
        if op not in EXPLAINABLE:
            raise ValueError(f"explain не підтримує {op}")
        rollback = False
        if op == "S1":
            sql, params = S1_SQL, (kw["trip_number"],)
        elif op == "S2":
            ps = kw.get("page_size", 20)
            sql, params = S2_SQL, (kw["driver_id"], _z(kw["date_from"]), _z(kw["date_to"]), kw["page"] * ps, ps)
        elif op == "S5a":
            a, b = year_bounds(kw["year"])
            sql, params = S5A_SQL, (_z(a), _z(b))
        elif op == "S5b":
            sql, params = S5B_SQL, _s5b_params(kw["year"])
        elif op == "S6":
            sql = S6_OPEN_SQL if kw.get("scope", "all") == "open" else S6_ALL_SQL
            params, rollback = (kw["new_name"], kw["customer_id"]), True
        else:
            sql = S7_SQL
            params = (json.dumps([{"adr_class": kw["adr_class"]}]), _z(kw["date_from"]), _z(kw["date_to"]))
        return pg_explain(self.conn, sql, tuple(params), rollback)

    # ---- службові
    def delete_trips(self, trip_numbers: list[str]) -> int:
        return self.conn.execute(DELETE_SQL, (list(trip_numbers),)).rowcount

    def count(self) -> dict[str, int]:
        q = lambda s: int(self.conn.execute(s).fetchone()[0] or 0)
        ev = "FROM bench.trip_docs d, jsonb_array_elements(d.doc->'events') e WHERE e->>'type'="
        return {"trips": q("SELECT count(*) FROM bench.trip_docs"),
                "cargo": q("SELECT sum(jsonb_array_length(doc->'cargo')) FROM bench.trip_docs"),
                "events": q("SELECT sum(jsonb_array_length(doc->'events')) FROM bench.trip_docs"),
                "fuel": q(f"SELECT count(*) {ev}'fuel'"),
                "incidents": q(f"SELECT count(*) {ev}'incident'")}
