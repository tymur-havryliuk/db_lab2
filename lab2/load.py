"""Завантаження згенерованих рейсів у SQL, JSONB і Mongo із замірами часу.

Запуск: uv run python -m lab2.load --scale {10k,100k,500k,1m} [--variants sql,jsonb,mongo] [--seed 42]
Фази в results/{scale}/load.csv: generate, data, indexes, analyze (тільки PG).
total це data + indexes + analyze, без generate.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from itertools import islice

from . import schema
from .codec import SQL_COLUMNS, TRIP_TABLES, refs_rows, to_jsonb, to_mongo, to_sql_rows
from .config import ROOT
from .db import mongo_trips, pg_connect
from .generator import REF_SEED, build_refs, generate_trips

SCALES = {"10k": 10_000, "100k": 100_000, "500k": 500_000, "1m": 1_000_000}
ALL_VARIANTS = ("sql", "jsonb", "mongo")
SQL_CHUNK = 20_000
MONGO_BATCH = 2_000
REF_TABLES = ("cities", "routes", "vehicles", "drivers", "customers")


def _copy_stmt(table: str) -> str:
    return f"COPY bench.{table} ({', '.join(SQL_COLUMNS[table])}) FROM STDIN"


def _chunks(it, size):
    while True:
        t = time.perf_counter()
        chunk = list(islice(it, size))
        yield chunk, time.perf_counter() - t
        if not chunk:
            return


def _split_index_sql() -> tuple[list[str], list[str]]:
    """Розбиває sql/02_indexes.sql на індекси нормалізованих таблиць і JSONB."""
    text = (ROOT / "sql" / "02_indexes.sql").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    sql_idx = [s for s in stmts if "bench.trip_docs" not in s]
    jsonb_idx = [s for s in stmts if "bench.trip_docs" in s]
    return sql_idx, jsonb_idx


def _analyze(tables: list[str]) -> float:
    t = time.perf_counter()
    with pg_connect(autocommit=True) as conn:
        for tb in tables:
            conn.execute(f"VACUUM ANALYZE bench.{tb}")
        conn.execute("CHECKPOINT")
    return time.perf_counter() - t


def _run_index_stmts(stmts: list[str]) -> float:
    t = time.perf_counter()
    with pg_connect(autocommit=True) as conn:
        for s in stmts:
            conn.execute(s)
    return time.perf_counter() - t


# --------------------------------------------------------------------------- варіанти
def load_sql(n: int, seed: int) -> dict:
    gen_t = data_t = 0.0
    counts = dict.fromkeys(TRIP_TABLES, 0)
    conn = pg_connect()
    t = time.perf_counter()
    for tb, rows in refs_rows(build_refs(REF_SEED)).items():
        with conn.cursor() as cur, cur.copy(_copy_stmt(tb)) as cp:
            for row in rows:
                cp.write_row(row)
    conn.commit()
    data_t += time.perf_counter() - t
    for chunk, g in _chunks(generate_trips(n, seed), SQL_CHUNK):
        gen_t += g
        if not chunk:
            break
        t = time.perf_counter()
        buckets = {tb: [] for tb in TRIP_TABLES}
        for doc in chunk:
            for tb, rows in to_sql_rows(doc).items():
                buckets[tb].extend(rows)
        with conn.cursor() as cur:
            for tb in TRIP_TABLES:  # через зовнішні ключі
                with cur.copy(_copy_stmt(tb)) as cp:
                    for row in buckets[tb]:
                        cp.write_row(row)
                counts[tb] += len(buckets[tb])
        conn.commit()
        data_t += time.perf_counter() - t
    conn.close()
    idx_t = _run_index_stmts(_split_index_sql()[0])
    an_t = _analyze(list(REF_TABLES) + list(TRIP_TABLES))
    return {"generate": gen_t, "data": data_t, "indexes": idx_t, "analyze": an_t,
            "docs": n, "rows": sum(counts.values())}


def load_jsonb(n: int, seed: int) -> dict:
    gen_t = data_t = 0.0
    conn = pg_connect()
    for chunk, g in _chunks(generate_trips(n, seed), SQL_CHUNK):
        gen_t += g
        if not chunk:
            break
        t = time.perf_counter()
        with conn.cursor() as cur, cur.copy("COPY bench.trip_docs (trip_number, doc) FROM STDIN") as cp:
            for doc in chunk:
                cp.write_row((doc["trip_number"], to_jsonb(doc)))
        conn.commit()
        data_t += time.perf_counter() - t
    conn.close()
    idx_t = _run_index_stmts(_split_index_sql()[1])
    an_t = _analyze(["trip_docs"])
    return {"generate": gen_t, "data": data_t, "indexes": idx_t, "analyze": an_t, "docs": n, "rows": n}


def load_mongo(n: int, seed: int) -> dict:
    schema.reset_mongo()
    schema.drop_mongo_indexes()
    coll = mongo_trips()
    gen_t = data_t = 0.0
    for chunk, g in _chunks(generate_trips(n, seed), MONGO_BATCH * 5):
        gen_t += g
        if not chunk:
            break
        t = time.perf_counter()
        docs = [to_mongo(d) for d in chunk]
        for i in range(0, len(docs), MONGO_BATCH):
            coll.insert_many(docs[i:i + MONGO_BATCH], ordered=False)
        data_t += time.perf_counter() - t
    t = time.perf_counter()
    schema.create_mongo_indexes()
    idx_t = time.perf_counter() - t
    return {"generate": gen_t, "data": data_t, "indexes": idx_t, "analyze": None, "docs": n, "rows": n}


# --------------------------------------------------------------------------- звірка
def counts_sql() -> dict:
    with pg_connect(autocommit=True) as conn:
        q = lambda s: conn.execute(s).fetchone()[0]
        return {"trips": q("SELECT count(*) FROM bench.trips"),
                "cargo": q("SELECT count(*) FROM bench.trip_cargo_items"),
                "events": q("SELECT count(*) FROM bench.trip_events"),
                "fuel": q("SELECT count(*) FROM bench.trip_events WHERE type='fuel'"),
                "fuel_tx": q("SELECT count(*) FROM bench.fuel_transactions"),
                "incidents": q("SELECT count(*) FROM bench.incidents")}


def counts_jsonb() -> dict:
    with pg_connect(autocommit=True) as conn:
        q = lambda s: int(conn.execute(s).fetchone()[0] or 0)
        ev = "FROM bench.trip_docs d, jsonb_array_elements(d.doc->'events') e WHERE e->>'type'="
        return {"trips": q("SELECT count(*) FROM bench.trip_docs"),
                "cargo": q("SELECT sum(jsonb_array_length(doc->'cargo')) FROM bench.trip_docs"),
                "events": q("SELECT sum(jsonb_array_length(doc->'events')) FROM bench.trip_docs"),
                "fuel": q(f"SELECT count(*) {ev}'fuel'"),
                "fuel_tx": q(f"SELECT count(*) {ev}'fuel'"),
                "incidents": q(f"SELECT count(*) {ev}'incident'")}


def counts_mongo() -> dict:
    def n_type(t):
        return {"$size": {"$filter": {"input": "$events", "as": "e", "cond": {"$eq": ["$$e.type", t]}}}}
    r = list(mongo_trips().aggregate([{"$group": {
        "_id": None, "trips": {"$sum": 1}, "cargo": {"$sum": {"$size": "$cargo"}},
        "events": {"$sum": {"$size": "$events"}}, "fuel": {"$sum": n_type("fuel")},
        "incidents": {"$sum": n_type("incident")}}}], allowDiskUse=True))
    r = r[0] if r else dict.fromkeys(("trips", "cargo", "events", "fuel", "incidents"), 0)
    return {"trips": r["trips"], "cargo": r["cargo"], "events": r["events"], "fuel": r["fuel"],
            "fuel_tx": r["fuel"], "incidents": r["incidents"]}


COUNTERS = {"sql": counts_sql, "jsonb": counts_jsonb, "mongo": counts_mongo}
LOADERS = {"sql": load_sql, "jsonb": load_jsonb, "mongo": load_mongo}


def print_stats(n: int, seed: int) -> None:
    comp = nc = fuel = inc = trips = cargo = 0
    for d in generate_trips(n, seed):
        trips += 1
        cargo += len(d["cargo"])
        if d["status"] == "completed":
            comp += 1
            nc += len(d["events"])
            fuel += sum(e["type"] == "fuel" for e in d["events"])
            inc += any(e["type"] == "incident" for e in d["events"])
    print(f"Середні: вантажу/рейс={cargo / trips:.2f}, подій/completed={nc / comp:.2f}, "
          f"заправок/completed={fuel / comp:.2f}, completed з інцидентом={inc / comp:.1%}")


def write_csv(scale: str, results: dict[str, dict]) -> None:
    path = ROOT / "results" / scale / "load.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["variant"] not in results]
    for variant, res in results.items():
        phases = [("generate", res["generate"]), ("data", res["data"]), ("indexes", res["indexes"])]
        if res["analyze"] is not None:
            phases.append(("analyze", res["analyze"]))
        total = res["data"] + res["indexes"] + (res["analyze"] or 0.0)
        phases.append(("total", total))
        for ph, sec in phases:
            rows.append({"scale": scale, "variant": variant, "phase": ph, "seconds": f"{sec:.3f}",
                         "docs": res["docs"], "rows": res["rows"]})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["scale", "variant", "phase", "seconds", "docs", "rows"])
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scale", required=True, choices=list(SCALES))
    ap.add_argument("--variants", default=",".join(ALL_VARIANTS))
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    bad = [v for v in variants if v not in ALL_VARIANTS]
    if bad:
        ap.error(f"невідомі варіанти: {bad}")
    n = SCALES[a.scale]

    if "sql" in variants or "jsonb" in variants:
        schema.reset_sql()  # скидає обидва варіанти в PG
        schema.drop_sql_indexes()
    results = {}
    for v in variants:
        print(f"[{v}] завантаження {n} рейсів...", flush=True)
        results[v] = LOADERS[v](n, a.seed)
        r = results[v]
        print(f"[{v}] generate={r['generate']:.1f}s data={r['data']:.1f}s indexes={r['indexes']:.1f}s "
              f"analyze={(r['analyze'] or 0):.1f}s", flush=True)
    write_csv(a.scale, results)

    got = {v: COUNTERS[v]() for v in variants}
    for v, c in got.items():
        print(f"Кількості [{v}]: {c}")
    ok = True
    ref = got[variants[0]]
    for v in variants:
        c = got[v]
        if c["trips"] != n:
            ok = False
            print(f"НЕЗБІГ: {v} trips={c['trips']} != {n}")
        for k in ("trips", "cargo", "events", "fuel", "incidents"):
            if c[k] != ref[k]:
                ok = False
                print(f"НЕЗБІГ: {k}: {variants[0]}={ref[k]} {v}={c[k]}")
        if v == "sql" and (c["fuel_tx"] != c["fuel"]):
            ok = False
            print("НЕЗБІГ: fuel_transactions != fuel-події у trip_events")
    if n <= 100_000:
        print_stats(n, a.seed)
    print("Звірка кількостей:", "OK" if ok else "ПОМИЛКА")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
