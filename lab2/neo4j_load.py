"""Завантаження графа в Neo4j: довідники з build_refs() і рейси з generate_trips(n).

Запуск: uv run python -m lab2.neo4j_load [--trips 10000] [--source generator|pg]
За замовчуванням 10 000 рейсів, для графових запитів цього достатньо.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .db import neo4j_driver, pg_connect
from .generator import build_refs, generate_trips

ROOT = Path(__file__).resolve().parent.parent
BATCH = 5000


def split_statements(text: str) -> list[str]:
    """Прибирає `//`-коментарі й ділить текст на оператори по `;`."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def batches(it, size: int):
    buf: list = []
    for x in it:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def run_batches(session, query: str, rows, size: int = BATCH) -> int:
    n = 0
    for chunk in batches(rows, size):
        session.execute_write(lambda tx, c=chunk: tx.run(query, rows=c).consume())
        n += len(chunk)
    return n


Q_CITY = "UNWIND $rows AS r MERGE (c:City {id: r.id}) SET c.name = r.name, c.oblast = r.oblast"
# Маршрути (a,b) і (b,a) зведені до одного ребра з мінімальним km, запити читають його без напрямку.
Q_ROAD = ("UNWIND $rows AS r MATCH (a:City {id: r.a}), (b:City {id: r.b}) "
          "CREATE (a)-[:ROAD {km: r.km, hours: r.hours}]->(b)")
Q_DRIVER = ("UNWIND $rows AS r MERGE (d:Driver {id: r.id}) "
            "SET d.full_name = r.full_name, d.license = r.license")
Q_VEHICLE = ("UNWIND $rows AS r MERGE (v:Vehicle {id: r.id}) "
             "SET v.plate = r.plate, v.vin = r.vin, v.model = r.model")
Q_CUSTOMER = ("UNWIND $rows AS r MERGE (c:Customer {id: r.id}) "
              "SET c.name = r.name, c.tax_code = r.tax_code "
              "WITH c, r MATCH (city:City {id: r.city_id}) MERGE (c)-[:LOCATED_IN]->(city)")
Q_TRIP = """
UNWIND $rows AS r
CREATE (t:Trip {id: r.id, number: r.number, status: r.status, planned_start: r.planned_start,
                route_code: r.route_code, km: r.km, distance_km: r.distance_km,
                freight_price: r.freight_price, fuel_l: r.fuel_l})
WITH t, r
MATCH (d:Driver {id: r.driver_id}), (c:Customer {id: r.customer_id}), (v:Vehicle {id: r.vehicle_id}),
      (f:City {id: r.from_id}), (to:City {id: r.to_id})
CREATE (d)-[:DROVE]->(t), (t)-[:FOR]->(c), (t)-[:USED]->(v), (t)-[:FROM]->(f), (t)-[:TO]->(to)
"""


def trip_rows(n: int, route_cities: dict[int, tuple[int, int]]):
    for t in generate_trips(n):
        f, to = route_cities[t["route"]["id"]]
        yield {
            "id": t["trip_id"], "number": t["trip_number"], "status": t["status"],
            "planned_start": t["planned_start"], "route_code": t["route"]["code"],
            "km": float(t["route"]["km"]), "distance_km": t["totals"]["distance_km"],
            "freight_price": float(t["totals"]["freight_price"]), "fuel_l": float(t["totals"]["fuel_l"]),
            "driver_id": t["driver"]["id"], "customer_id": t["customer"]["id"],
            "vehicle_id": t["vehicle"]["id"], "from_id": f, "to_id": to,
        }


def trip_rows_pg(n: int):
    """Перші n рейсів з bench.trips, щоб граф збігався з SQL-даними."""
    q = """SELECT t.trip_id, t.trip_number, t.status::text, t.planned_start, r.code, r.distance_km::float8,
                  t.distance_km, t.freight_price::float8, t.driver_id, t.customer_id, t.vehicle_id,
                  r.from_city_id, r.to_city_id
           FROM bench.trips t JOIN bench.routes r USING (route_id)
           WHERE t.trip_id <= %s ORDER BY t.trip_id"""
    with pg_connect(autocommit=True) as pg:
        pg.read_only = True
        for x in pg.execute(q, (n,)).fetchall():
            yield {"id": x[0], "number": x[1], "status": x[2], "planned_start": x[3], "route_code": x[4],
                   "km": x[5], "distance_km": x[6], "freight_price": x[7], "fuel_l": None,
                   "driver_id": x[8], "customer_id": x[9], "vehicle_id": x[10], "from_id": x[11], "to_id": x[12]}


def road_rows(refs) -> list[dict]:
    best: dict[tuple[int, int], dict] = {}
    for r in refs.routes:
        a, b = sorted((r["from_city_id"], r["to_city_id"]))
        km = float(r["distance_km"])
        if (a, b) not in best or km < best[(a, b)]["km"]:
            best[(a, b)] = {"a": a, "b": b, "km": km, "hours": float(r["duration_hours"])}
    return list(best.values())


def clear(session) -> None:
    session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()


def print_counts(session) -> None:
    print("Вузли:")
    for rec in session.run("MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c ORDER BY l"):
        print(f"  {rec['l']:<10}{rec['c']:>8}")
    print("Ребра:")
    for rec in session.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t"):
        print(f"  {rec['t']:<10}{rec['c']:>8}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Завантаження графа в Neo4j")
    ap.add_argument("--trips", type=int, default=10_000, help="кількість рейсів (за замовч. 10000)")
    ap.add_argument("--source", choices=["generator", "pg"], default="generator",
                    help="рейси: generate_trips(n) або перші n з PostgreSQL bench.trips (для lab2.graph_compare)")
    args = ap.parse_args()

    t0 = time.perf_counter()
    refs = build_refs()
    route_cities = {r["id"]: (r["from_city_id"], r["to_city_id"]) for r in refs.routes}
    with neo4j_driver() as drv, drv.session() as s:
        print("Очищення графа...")
        clear(s)
        print("Схема (neo4j/schema.cypher)...")
        for stmt in split_statements((ROOT / "neo4j" / "schema.cypher").read_text(encoding="utf-8")):
            s.run(stmt).consume()

        run_batches(s, Q_CITY, refs.cities)
        n_roads = run_batches(s, Q_ROAD, road_rows(refs))
        run_batches(s, Q_DRIVER, refs.drivers)
        run_batches(s, Q_VEHICLE, refs.vehicles)
        run_batches(s, Q_CUSTOMER, refs.customers)
        print(f"Довідники: {len(refs.cities)} міст, {n_roads} доріг, {len(refs.drivers)} водіїв, "
              f"{len(refs.vehicles)} авто, {len(refs.customers)} замовників")

        n = run_batches(s, Q_TRIP, trip_rows_pg(args.trips) if args.source == "pg"
                       else trip_rows(args.trips, route_cities))
        print(f"Рейси: {n}")
        print_counts(s)
    print(f"Готово за {time.perf_counter() - t0:.1f} с")


if __name__ == "__main__":
    main()
