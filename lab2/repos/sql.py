"""Варіант `sql`: нормалізована схема PostgreSQL (bench.*)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg.types.json import set_json_loads

from .. import codec
from ..db import pg_connect
from .base import EXPLAINABLE, ServerPlan

UTC = timezone.utc
D1 = Decimal("0.1")
D2 = Decimal("0.01")


def utc(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.astimezone(UTC)


def year_bounds(year: int) -> tuple[datetime, datetime]:
    return datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)


def money(liters: Decimal, price: Decimal) -> Decimal:
    """round(liters*price, 2) від нуля, як round(numeric) у PG і генератор."""
    return (liters * price).quantize(D2, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ SQL-константи
S1_SQL = """
SELECT t.trip_id, t.trip_number, t.status::text,
       v.vehicle_id, v.plate_number, v.vin, v.model,
       d.driver_id, concat_ws(' ', d.last_name, d.first_name, d.middle_name), d.license_number,
       c.customer_id, c.name, c.tax_code, cc.name,
       r.route_id, r.code, cf.name, ct.name, r.distance_km,
       t.planned_start, t.planned_end, t.started_at, t.finished_at, t.cancel_reason,
       t.distance_km, t.freight_price,
       (SELECT coalesce(sum(f.quantity), 0) FROM bench.fuel_transactions f WHERE f.trip_id = t.trip_id),
       (SELECT coalesce(sum(f.total_cost), 0) FROM bench.fuel_transactions f WHERE f.trip_id = t.trip_id),
       (SELECT json_agg(json_build_object(
                  'kind', ci.kind, 'title', ci.title, 'kg', ci.weight_kg, 'packages', ci.packages,
                  'temp_min', ci.temp_min, 'temp_max', ci.temp_max, 'un_number', ci.un_number,
                  'adr_class', ci.adr_class, 'packing_group', ci.packing_group,
                  'length_cm', ci.length_cm, 'width_cm', ci.width_cm, 'height_cm', ci.height_cm)
                  ORDER BY ci.line_no)
          FROM bench.trip_cargo_items ci WHERE ci.trip_id = t.trip_id),
       (SELECT json_agg(json_build_object(
                  'type', e.type, 'at', e.occurred_at, 'odometer', e.odometer_km,
                  'location', e.location, 'minutes', e.duration_min, 'reason', e.reason,
                  'receiver', e.receiver_name,
                  'liters', f.quantity, 'unit_price', f.unit_price, 'station', f.station,
                  'incident_type', i.type, 'description', i.description, 'amount', i.amount,
                  'at_fault', i.driver_at_fault)
                  ORDER BY e.seq)
          FROM bench.trip_events e
          LEFT JOIN bench.fuel_transactions f ON f.trip_id = e.trip_id AND f.event_seq = e.seq
          LEFT JOIN bench.incidents i ON i.trip_id = e.trip_id AND i.event_seq = e.seq
         WHERE e.trip_id = t.trip_id)
  FROM bench.trips t
  JOIN bench.vehicles  v  ON v.vehicle_id  = t.vehicle_id
  JOIN bench.drivers   d  ON d.driver_id   = t.driver_id
  JOIN bench.customers c  ON c.customer_id = t.customer_id
  LEFT JOIN bench.cities cc ON cc.city_id  = c.city_id
  JOIN bench.routes    r  ON r.route_id    = t.route_id
  JOIN bench.cities    cf ON cf.city_id    = r.from_city_id
  JOIN bench.cities    ct ON ct.city_id    = r.to_city_id
 WHERE t.trip_number = %s
"""

S2_SQL = """
SELECT t.trip_number, t.planned_start, t.status::text, cf.name, ct.name, c.name, t.freight_price
  FROM bench.trips t
  JOIN bench.routes    r  ON r.route_id    = t.route_id
  JOIN bench.cities    cf ON cf.city_id    = r.from_city_id
  JOIN bench.cities    ct ON ct.city_id    = r.to_city_id
  JOIN bench.customers c  ON c.customer_id = t.customer_id
 WHERE t.driver_id = %s AND t.planned_start >= %s AND t.planned_start < %s
 ORDER BY t.planned_start DESC, t.trip_number DESC
OFFSET %s LIMIT %s
"""

S3_LOCK_SQL = "SELECT trip_id, vehicle_id, driver_id FROM bench.trips WHERE trip_number = %s FOR UPDATE"
S3_SEQ_SQL = "SELECT coalesce(max(seq), 0) + 1 FROM bench.trip_events WHERE trip_id = %s"
S3_FUNC_SQL = "SELECT bench.append_trip_event(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
COPY_MIN = 50   # від якого розміру батча S4 використовує COPY (SQL і JSONB)

S5A_SQL = """
SELECT customer_id, extract(month FROM planned_start)::int, count(*), sum(freight_price)
  FROM bench.trips
 WHERE status = 'completed' AND planned_start >= %s AND planned_start < %s
 GROUP BY 1, 2 ORDER BY 1, 2
"""

S5B_SQL = """
SELECT vehicle_id, count(*), sum(quantity), sum(total_cost)
  FROM bench.fuel_transactions
 WHERE filled_at >= %s AND filled_at < %s
 GROUP BY 1 ORDER BY 1
"""

S6_SQL = "UPDATE bench.customers SET name = %s WHERE customer_id = %s"

S7_SQL = """
SELECT trip_number, planned_start
  FROM bench.trips t
 WHERE planned_start >= %s AND planned_start < %s
   AND EXISTS (SELECT 1 FROM bench.trip_cargo_items c
                WHERE c.trip_id = t.trip_id AND c.kind = 'adr' AND c.adr_class = %s)
 ORDER BY planned_start, trip_number
"""

DELETE_SQL = "DELETE FROM bench.trips WHERE trip_number = ANY(%s)"


# ------------------------------------------------------------------ спільне для обох PG-варіантів
def _walk(node: dict, acc: list[dict]) -> list[dict]:
    acc.append(node)
    for ch in node.get("Plans", []):
        _walk(ch, acc)
    return acc


def pg_explain(conn, sql: str, params: tuple, rollback: bool = False) -> ServerPlan:
    """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) для того самого запиту і параметрів.

    server_ms це Planning Time + Execution Time. Для DML rollback=True: ANALYZE справді виконує UPDATE, тому відкочуємо.
    """
    stmt = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql
    with conn.transaction(force_rollback=rollback):
        raw = conn.execute(stmt, params).fetchone()[0]
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    raw = json.loads(json.dumps(raw, default=float))   # щоб у плані не було Decimal
    top = raw[0]
    root = top["Plan"]
    nodes = _walk(root, [])
    planning, execution = float(top.get("Planning Time", 0)), float(top.get("Execution Time", 0))
    notes = {
        "planning_ms": planning, "execution_ms": execution,
        "shared_hit": root.get("Shared Hit Blocks", 0), "shared_read": root.get("Shared Read Blocks", 0),
        "top_node": root["Node Type"],
        "indexes": sorted({n["Index Name"] for n in nodes if "Index Name" in n}),
        "scans": sorted({n["Node Type"] for n in nodes if "Scan" in n["Node Type"]}),
    }
    return ServerPlan(server_ms=planning + execution, plan=raw, notes=notes)


# ------------------------------------------------------------------ репозиторій
class SqlRepository:
    name = "sql"

    def __init__(self) -> None:
        self.conn = pg_connect(autocommit=True)
        # числа з json_agg читаємо як Decimal, а не float
        set_json_loads(lambda s: json.loads(s, parse_float=Decimal), self.conn)

    def close(self) -> None:
        self.conn.close()

    # ---- S1
    def get_trip(self, trip_number: str) -> dict | None:
        row = self.conn.execute(S1_SQL, (trip_number,)).fetchone()
        if row is None:
            return None
        (trip_id, tn, status, veh_id, plate, vin, model, drv_id, full_name, lic,
         cus_id, cus_name, tax, cus_city, rt_id, rt_code, rt_from, rt_to, rt_km,
         ps, pe, started, finished, cancel, distance, freight, fuel_l, fuel_cost, cargo, events) = row
        return {
            "trip_number": tn, "trip_id": trip_id, "status": status,
            "vehicle": {"id": veh_id, "plate": plate, "vin": vin, "model": model},
            "driver": {"id": drv_id, "full_name": full_name, "license": lic},
            "customer": {"id": cus_id, "name": cus_name, "tax_code": tax, "city": cus_city},
            "route": {"id": rt_id, "code": rt_code, "from": rt_from, "to": rt_to, "km": rt_km.quantize(D1)},
            "planned_start": utc(ps), "planned_end": utc(pe),
            "started_at": utc(started), "finished_at": utc(finished),
            "cancel_reason": cancel,
            "cargo": [_cargo(c) for c in cargo or []],
            "events": [_event(e) for e in events or []],
            "totals": {"distance_km": distance, "fuel_l": Decimal(fuel_l).quantize(D2),
                       "fuel_cost": Decimal(fuel_cost).quantize(D2),
                       "freight_price": freight.quantize(D2)},
        }

    # ---- S2
    def driver_trips(self, driver_id, date_from, date_to, page, page_size=20) -> list[tuple]:
        return self.conn.execute(
            S2_SQL, (driver_id, date_from, date_to, page * page_size, page_size)).fetchall()

    # ---- S3
    def append_event(self, trip_number: str, event: dict) -> None:
        """Один виклик PL/pgSQL-функції з sql/04_functions.sql (один запит до бази, як $push у Mongo)."""
        e = event
        self.conn.execute(S3_FUNC_SQL, (
            trip_number, e["type"], e["at"], e.get("odometer"), e.get("location"), e.get("minutes"),
            e.get("reason"), e.get("receiver"), e.get("liters"), e.get("unit_price"), e.get("station"),
            e.get("incident_type"), e.get("description"), e.get("amount"), e.get("at_fault")))

    def append_event_multi(self, trip_number: str, event: dict) -> None:
        """Перша версія S3 для абляції: ті самі кроки окремими запитами в транзакції (5-6 запитів)."""
        with self.conn.transaction():
            row = self.conn.execute(S3_LOCK_SQL, (trip_number,)).fetchone()
            if row is None:
                raise LookupError(f"Рейс {trip_number} не знайдено")
            trip_id, veh, drv = row
            seq = self.conn.execute(S3_SEQ_SQL, (trip_id,)).fetchone()[0]
            t, at = event["type"], event["at"]
            ex = self.conn.execute
            ex("INSERT INTO bench.trip_events (trip_id, seq, type, occurred_at, odometer_km, location, "
               "duration_min, reason, receiver_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (trip_id, seq, t, at, event.get("odometer"), event.get("location"), event.get("minutes"),
                event.get("reason"), event.get("receiver")))
            if t == "fuel":
                ex("INSERT INTO bench.fuel_transactions (trip_id, event_seq, vehicle_id, driver_id, filled_at, "
                   "quantity, unit_price, odometer_km, station) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (trip_id, seq, veh, drv, at, event["liters"], event["unit_price"], event["odometer"],
                    event.get("station")))
            elif t == "incident":
                ex("INSERT INTO bench.incidents (trip_id, event_seq, vehicle_id, driver_id, type, occurred_at, "
                   "description, amount, driver_at_fault) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (trip_id, seq, veh, drv, event["incident_type"], at, event["description"],
                    event["amount"], event["at_fault"]))

    # ---- S4
    def insert_trips(self, docs: list[dict]) -> None:
        """Малі батчі через executemany, від COPY_MIN документів через COPY у кожну таблицю."""
        (self.insert_trips_copy if len(docs) >= COPY_MIN else self.insert_trips_executemany)(docs)

    def insert_trips_copy(self, docs: list[dict]) -> None:
        batch: dict[str, list[tuple]] = {t: [] for t in codec.TRIP_TABLES}
        for doc in docs:
            for table, rows in codec.to_sql_rows(doc).items():
                batch[table].extend(rows)
        with self.conn.transaction(), self.conn.cursor() as cur:
            for table in codec.TRIP_TABLES:        # порядок через зовнішні ключі
                if batch[table]:
                    with cur.copy(f"COPY bench.{table} ({', '.join(codec.SQL_COLUMNS[table])}) FROM STDIN") as cp:
                        for row in batch[table]:
                            cp.write_row(row)

    def insert_trips_executemany(self, docs: list[dict]) -> None:
        batch: dict[str, list[tuple]] = {t: [] for t in codec.TRIP_TABLES}
        for doc in docs:
            for table, rows in codec.to_sql_rows(doc).items():
                batch[table].extend(rows)
        with self.conn.transaction(), self.conn.cursor() as cur:
            for table in codec.TRIP_TABLES:        # порядок через зовнішні ключі
                if not batch[table]:
                    continue
                cols = codec.SQL_COLUMNS[table]
                cur.executemany(
                    f"INSERT INTO bench.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                    batch[table])

    # ---- S5
    def revenue_by_customer_month(self, year: int) -> list[tuple]:
        return self.conn.execute(S5A_SQL, year_bounds(year)).fetchall()

    def fuel_by_vehicle(self, year: int) -> list[tuple]:
        return self.conn.execute(S5B_SQL, year_bounds(year)).fetchall()

    # ---- S6
    def rename_customer(self, customer_id: int, new_name: str, scope: str = "all") -> int:
        # У нормалізованій схемі назва одна для обох scope, оновлюється один рядок.
        return self.conn.execute(S6_SQL, (new_name, customer_id)).rowcount

    # ---- S7
    def adr_trips(self, adr_class: int, date_from: datetime, date_to: datetime) -> list[tuple]:
        return self.conn.execute(S7_SQL, (date_from, date_to, adr_class)).fetchall()

    # ---- explain
    def explain(self, op: str, **kw: Any) -> ServerPlan:
        if op not in EXPLAINABLE:
            raise ValueError(f"explain не підтримує {op}")
        rollback = False
        if op == "S1":
            sql, params = S1_SQL, (kw["trip_number"],)
        elif op == "S2":
            ps = kw.get("page_size", 20)
            sql, params = S2_SQL, (kw["driver_id"], kw["date_from"], kw["date_to"], kw["page"] * ps, ps)
        elif op == "S5a":
            sql, params = S5A_SQL, year_bounds(kw["year"])
        elif op == "S5b":
            sql, params = S5B_SQL, year_bounds(kw["year"])
        elif op == "S6":
            sql, params, rollback = S6_SQL, (kw["new_name"], kw["customer_id"]), True
        else:
            sql, params = S7_SQL, (kw["date_from"], kw["date_to"], kw["adr_class"])
        return pg_explain(self.conn, sql, tuple(params), rollback)

    # ---- службові
    def delete_trips(self, trip_numbers: list[str]) -> int:
        return self.conn.execute(DELETE_SQL, (list(trip_numbers),)).rowcount

    def count(self) -> dict[str, int]:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {"trips": q("SELECT count(*) FROM bench.trips"),
                "cargo": q("SELECT count(*) FROM bench.trip_cargo_items"),
                "events": q("SELECT count(*) FROM bench.trip_events"),
                "fuel": q("SELECT count(*) FROM bench.fuel_transactions"),
                "incidents": q("SELECT count(*) FROM bench.incidents")}


# ------------------------------------------------------------------ збирання елементів документа
_CARGO_KEYS = {
    "general": ("packages",), "reefer": ("temp_min", "temp_max"),
    "adr": ("un_number", "adr_class", "packing_group"),
    "oversize": ("length_cm", "width_cm", "height_cm"),
}
_EVENT_KEYS = {
    "departed": ("odometer",), "checkpoint": ("odometer", "location"),
    "fuel": ("odometer", "liters", "unit_price", "station"), "idle": ("minutes", "reason"),
    "incident": ("incident_type", "description", "amount", "at_fault"),
    "delivered": ("odometer", "receiver"),
}


def _cargo(c: dict) -> dict:
    out = {"kind": c["kind"], "title": c["title"], "kg": c["kg"]}
    out.update((k, c[k]) for k in _CARGO_KEYS[c["kind"]])
    return out


def _event(e: dict) -> dict:
    out = {"type": e["type"], "at": codec._parse_dt(e["at"])}
    for k in _EVENT_KEYS[e["type"]]:
        v = e[k]
        out[k] = v.quantize(D2) if k in ("liters", "unit_price", "amount") else v
    return out
