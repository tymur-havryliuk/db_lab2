"""Бенчмарк S1-S8 для трьох варіантів зберігання.

Етапи: перевірка еквівалентності, теплий кеш, серверний час (EXPLAIN), холодний кеш, записи, S8.
Запуск: uv run python -m lab2.bench run --scale 10k [--only warm,server,cold,writes,s8] [--quick]
"""
from __future__ import annotations

import argparse
import gc
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from lab2 import equivalence as eq
from lab2.bench import concurrency
from lab2.bench.common import (RESULTS, SERVICE, VARIANTS, CsvSink, cold_restart, dump_json,
                               environment_info, summarize)
from lab2.repos import open_repo

UTC = timezone.utc

LAT_FIELDS = ["scale", "scenario", "variant", "cache", "iter", "client_us", "rows", "param"]
SUM_FIELDS = ["scale", "scenario", "variant", "cache", "n", "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms"]
SRV_FIELDS = ["scale", "scenario", "variant", "metric", "iter", "server_ms", "notes"]
S4_FIELDS = ["scale", "variant", "batch_size", "batch_idx", "docs", "client_us"]


def rows_of(res: Any) -> int:
    if res is None:
        return 0
    if isinstance(res, (list, tuple)):
        return len(res)
    if isinstance(res, int):
        return res
    return 1


def rotated(variants: list[str], k: int) -> list[str]:
    k %= len(variants)
    return variants[k:] + variants[:k]


def timed(fn: Callable[[], Any]) -> tuple[float, Any]:
    t0 = time.perf_counter_ns()
    res = fn()
    return (time.perf_counter_ns() - t0) / 1000, res


class Bench:
    def __init__(self, scale: str, variants: list[str], quick: bool, seed: int):
        self.scale, self.variants, self.quick, self.seed = scale, variants, quick, seed
        self.out = RESULTS / scale
        self.repos = {v: open_repo(v) for v in variants}
        self.universe = eq.load_universe(self.repos["sql"] if "sql" in self.repos else open_repo("sql"))
        self.lat = self.summary = self.srv = None
        # кількість вимірювань: точкових не менше 50, важких агрегацій менше
        self.n = {"S1": 200, "S2": 200, "S7": 200, "S5a": 15, "S5b": 15, "S3a": 200, "S3b": 200, "S6": 15}
        self.warm = {"S1": 30, "S2": 30, "S7": 30, "S5a": 2, "S5b": 2, "S3a": 20, "S3b": 20, "S6": 0}
        if quick:
            self.n = {k: max(5, v // 10) for k, v in self.n.items()}
            self.warm = {k: min(v, 3) for k, v in self.warm.items()}

    def begin(self, stage: str) -> None:
        """Кожен етап пише у свої файли `<stage>_*.csv`, повторний запуск перезаписує тільки їх."""
        self.close_sinks()
        self.out.mkdir(parents=True, exist_ok=True)
        for f in self.out.glob(f"{stage}_*.csv"):
            f.unlink()
        self.lat = CsvSink(self.out / f"{stage}_latency.csv", LAT_FIELDS)
        self.summary = CsvSink(self.out / f"{stage}_summary.csv", SUM_FIELDS)
        self.srv = CsvSink(self.out / f"{stage}_server.csv", SRV_FIELDS)

    def close_sinks(self) -> None:
        for s in (self.lat, self.summary, self.srv):
            if s is not None:
                s.close()

    def rng(self, scenario: str, stream: str) -> random.Random:
        return random.Random(f"{self.seed}:{scenario}:{stream}")

    def params(self, scenario: str, stream: str, n: int) -> list[dict]:
        r = self.rng(scenario, stream)
        return [eq.READ_PARAMS[scenario](r, self.universe) for _ in range(n)]

    def reconnect(self, variants: list[str]) -> None:
        for v in variants:
            try:
                self.repos[v].close()
            except Exception:  # після рестарту підключення вже не працює
                pass
            self.repos[v] = open_repo(v)

    def record(self, scenario: str, variant: str, cache: str, samples: list[tuple[float, int, Any]]) -> None:
        self.lat.writemany({"scale": self.scale, "scenario": scenario, "variant": variant, "cache": cache,
                            "iter": i, "client_us": round(us, 1), "rows": rows, "param": str(p)}
                           for i, (us, rows, p) in enumerate(samples))
        s = summarize(us for us, _, _ in samples)
        self.summary.writemany([{"scale": self.scale, "scenario": scenario, "variant": variant, "cache": cache,
                                 **{k: (round(x, 3) if isinstance(x, float) else x) for k, x in s.items()}}])
        print(f"  {scenario:4} {variant:5} {cache:4} n={s['n']:<4} p50={s['p50_ms']:9.3f} ms  "
              f"p95={s['p95_ms']:9.3f}  p99={s['p99_ms']:9.3f}", flush=True)

    # ---------- 0. коректність ----------
    def settle(self) -> None:
        """Між записними сценаріями: CHECKPOINT і VACUUM, щоб хвости одного варіанта не потрапили в заміри іншого."""
        from lab2.db import mongo_db, pg_connect
        with pg_connect(autocommit=True) as c:
            c.execute("VACUUM ANALYZE bench.trips, bench.trip_events, bench.fuel_transactions, bench.customers, bench.trip_docs")
            c.execute("CHECKPOINT")
        mongo_db().client.admin.command("fsync")
        time.sleep(2)

    def verify(self, tag: str = "") -> None:
        rep = eq.check_reads(self.repos, self.universe, seed=self.seed, n=5 if self.quick else 15)
        dump_json(self.out / f"equivalence{tag}.json", {"checked": rep.checked, "mismatches": rep.mismatches,
                                                   "empty_results": rep.empty_results})
        print(f"[verify] перевірено {rep.checked} викликів, розбіжностей: {len(rep.mismatches)}", flush=True)
        if not rep.ok:
            for m in rep.mismatches[:10]:
                print("   ", m)
            sys.exit("Результати варіантів різняться — заміри скасовано.")

    def sizes(self) -> None:
        from lab2.db import mongo_db, pg_connect
        rows = []
        with pg_connect() as c, c.cursor() as cur:
            cur.execute("""SELECT c.relname, pg_table_size(c.oid), pg_indexes_size(c.oid), c.reltuples::bigint
                           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname = 'bench' AND c.relkind = 'r' ORDER BY 1""")
            for name, tsz, isz, tup in cur.fetchall():
                rows.append({"variant": "jsonb" if name == "trip_docs" else "sql", "object": name,
                             "table_bytes": tsz, "index_bytes": isz, "rows": tup})
        st = mongo_db().command("collStats", "trips")
        rows.append({"variant": "mongo", "object": "trips", "table_bytes": st["storageSize"],
                     "index_bytes": st["totalIndexSize"], "rows": st["count"], "uncompressed_bytes": st["size"]})
        (self.out / "sizes.csv").unlink(missing_ok=True)
        sink = CsvSink(self.out / "sizes.csv", ["scale", "variant", "object", "table_bytes", "index_bytes",
                                                "rows", "uncompressed_bytes"])
        sink.writemany({"scale": self.scale, **r} for r in rows)
        sink.close()

    # ---------- 1. теплий кеш, читання ----------
    def warm_reads(self) -> None:
        print("[warm] читання", flush=True)
        for k, sc in enumerate(("S1", "S2", "S7", "S5a", "S5b")):
            warm_p = self.params(sc, "warmup", self.warm[sc])
            meas_p = self.params(sc, "measure", self.n[sc])
            for v in rotated(self.variants, k):
                repo, call = self.repos[v], eq.READ_CALLS[sc]
                for p in warm_p:
                    call(repo, p)
                samples = []
                gc.collect(); gc.disable()
                try:
                    for p in meas_p:
                        us, res = timed(lambda: call(repo, p))
                        samples.append((us, rows_of(res), p))
                finally:
                    gc.enable()
                self.record(sc, v, "warm", samples)

    # ---------- 2. серверний час і плани ----------
    def server_side(self) -> None:
        print("[server] EXPLAIN / executionStats", flush=True)
        k_runs = 5 if self.quick else 20
        for sc in ("S1", "S2", "S7", "S5a", "S5b"):
            ps = self.params(sc, "measure", self.n[sc])[: (5 if sc.startswith("S5") else k_runs)]
            for v in self.variants:
                repo = self.repos[v]
                for i, p in enumerate(ps):
                    plan = repo.explain(sc, **p)
                    self.srv.writemany([{"scale": self.scale, "scenario": sc, "variant": v, "metric": "explain", "iter": i,
                                         "server_ms": round(plan.server_ms, 3), "notes": plan.notes}])
                    if i == 0:
                        dump_json(self.out / "plans" / f"{sc}_{v}.json", {"params": p, "server_ms": plan.server_ms,
                                                                          "notes": plan.notes, "plan": plan.plan})
            if "mongo" in self.variants:
                self._mongo_profile(sc, ps)
        # план S6 потрібен лише щоб побачити використання індексу (Mongo explain запис не застосовує)
        cid = self.rng("S6", "plan").choice(self.universe["customers"])
        for v in self.variants:
            plan = self.repos[v].explain("S6", customer_id=cid, new_name="EXPLAIN", scope="all")
            dump_json(self.out / "plans" / f"S6_{v}.json", {"customer_id": cid, "server_ms": plan.server_ms,
                                                            "notes": plan.notes, "plan": plan.plan})

    def _mongo_profile(self, sc: str, ps: list[dict]) -> None:
        """Серверний час Mongo через профайлер.

        explain() дає executionTimeMillis з точністю 1 мс, тому швидкі запити виходять 0.
        Профайлер пише cpuNanos (сума за command і getMore). На теплому кеші це приблизно
        дорівнює реальному часу, тому порівнюється з Execution Time у PG. Профайлер увімкнений лише на цей прохід."""
        from lab2.db import mongo_db
        db, repo, call = mongo_db(), self.repos["mongo"], eq.READ_CALLS[sc]
        db.command("profile", 0)
        db.drop_collection("system.profile")
        db.create_collection("system.profile", capped=True, size=64 << 20)
        db.command("profile", 2)
        try:
            for i, p in enumerate(ps):
                marker = db.system.profile.count_documents({})
                call(repo, p)
                ents = [e for e in db.system.profile.find().sort("$natural", 1).skip(marker)
                        if e.get("ns", "").endswith(".trips")]
                cpu = sum(e.get("cpuNanos", 0) for e in ents) / 1e6
                self.srv.writemany([{"scale": self.scale, "scenario": sc, "variant": "mongo", "metric": "profiler_cpu",
                                     "iter": i, "server_ms": round(cpu, 4),
                                     "notes": {"millis": sum(e.get("millis", 0) for e in ents), "ops": len(ents),
                                               "docsExamined": sum(e.get("docsExamined", 0) for e in ents),
                                               "keysExamined": sum(e.get("keysExamined", 0) for e in ents),
                                               "planSummary": ents[0].get("planSummary") if ents else None}}])
        finally:
            db.command("profile", 0)

    # ---------- 3. холодний кеш ----------
    def cold(self) -> None:
        print("[cold] stop, drop_caches, start", flush=True)
        r_point = 5 if self.quick else 30
        restarts_heavy = 1 if self.quick else 3
        for v in self.variants:
            for sc in ("S1", "S2", "S7"):
                cold_restart(SERVICE[v]); self.reconnect([v])
                self.repos[v].count()          # відкрити підключення до виміру (count не чіпає дані рейсу)
                ps = self.params(sc, "cold", r_point)
                samples = []
                for p in ps:
                    us, res = timed(lambda: eq.READ_CALLS[sc](self.repos[v], p))
                    samples.append((us, rows_of(res), p))
                self.record(sc, v, "cold", samples)
            for sc in ("S5a", "S5b"):
                samples = []
                for p in self.params(sc, "cold", restarts_heavy):
                    cold_restart(SERVICE[v]); self.reconnect([v])
                    self.repos[v].count()
                    us, res = timed(lambda: eq.READ_CALLS[sc](self.repos[v], p))
                    samples.append((us, rows_of(res), p))
                self.record(sc, v, "cold", samples)
        # після холодних замірів повертаємо все як було
        self.reconnect(self.variants)

    # ---------- 4. записи ----------
    def writes(self) -> None:
        print("[writes] S3, S6, S4", flush=True)
        open_trips = self.universe["by_status"]["in_progress"]
        base_at = datetime(2026, 11, 5, tzinfo=UTC)
        for k, (sc, kind) in enumerate((("S3a", "checkpoint"), ("S3b", "fuel"))):
            r = self.rng(sc, "measure")
            trips = [r.choice(open_trips) for _ in range(self.n[sc] + self.warm[sc])]
            for v in rotated(self.variants, k):
                self.settle()
                repo, samples = self.repos[v], []
                for i, tn in enumerate(trips):
                    ev = self._event(kind, base_at + timedelta(minutes=i), i)
                    us, _ = timed(lambda: repo.append_event(tn, ev))
                    if i >= self.warm[sc]:
                        samples.append((us, 1, tn))
                self.record(sc, v, "warm", samples)
        self._s6()
        self._s4()

    @staticmethod
    def _event(kind: str, at: datetime, i: int) -> dict:
        if kind == "checkpoint":
            return {"type": "checkpoint", "at": at, "odometer": 500_000 + i, "location": f"Бенч-пункт {i % 50}"}
        return {"type": "fuel", "at": at, "odometer": 500_000 + i, "liters": Decimal("150.50"),
                "unit_price": Decimal("52.40"), "station": "Бенч АЗС"}

    def _s6(self) -> None:
        """Перейменування замовника: випадкові замовники і найбільший (найгірший для денормалізації).
        Для кожного виміру записується кількість змінених копій."""
        from lab2.db import pg_connect
        with pg_connect() as c, c.cursor() as cur:
            cur.execute("SELECT customer_id, name FROM bench.customers")
            names = dict(cur.fetchall())
            cur.execute("SELECT customer_id, count(*) FROM bench.trips GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
            top = cur.fetchone()[0]
        r = self.rng("S6", "measure")
        cust = r.sample(self.universe["customers"], self.n["S6"])
        sink = CsvSink(self.out / "writes_s6_raw.csv", ["scale", "scenario", "variant", "customer_id", "is_top",
                                                 "changed", "client_us"])
        try:
            for k, scope in enumerate(("all", "open")):
                sc = f"S6{scope}"
                for v in rotated(self.variants, k):
                    self.settle()
                    repo, samples = self.repos[v], []
                    for cid in cust + [top]:
                        us, changed = timed(lambda: repo.rename_customer(cid, names[cid] + " [нова назва]", scope))
                        sink.write({"scale": self.scale, "scenario": sc, "variant": v, "customer_id": cid,
                                    "is_top": cid == top, "changed": changed, "client_us": round(us, 1)})
                        if cid != top:
                            samples.append((us, changed, cid))
                    self.record(sc, v, "warm", samples)
        finally:
            for v in self.variants:     # повертаємо назви назад
                for cid in set(cust + [top]):
                    self.repos[v].rename_customer(cid, names[cid], "all")
            sink.close()

    def _s4(self) -> None:
        """Масова вставка: ті самі нові рейси батчами по 1, 10, 100 і 1000, кожен батч в одній транзакції.
        Після виміру вставлені рейси видаляються."""
        from lab2.generator import extra_trips
        plan = [(1, 200), (10, 100), (100, 30), (1000, 10)]
        if self.quick:
            plan = [(1, 20), (10, 10), (100, 5), (1000, 2)]
        total = sum(b * n for b, n in plan)
        docs = extra_trips(total, self.universe["max_trip_id"] + 1, seed=self.seed)
        sink = CsvSink(self.out / "writes_s4_raw.csv", S4_FIELDS)
        for k, v in enumerate(rotated(self.variants, 0)):
            self.settle()
            repo, pos = self.repos[v], 0
            try:
                for bsize, nb in plan:
                    lat = []
                    gc.collect(); gc.disable()
                    try:
                        for bi in range(nb):
                            batch = docs[pos:pos + bsize]; pos += bsize
                            us, _ = timed(lambda: repo.insert_trips(batch))
                            lat.append(us)
                            sink.write({"scale": self.scale, "variant": v, "batch_size": bsize, "batch_idx": bi,
                                        "docs": len(batch), "client_us": round(us, 1)})
                    finally:
                        gc.enable()
                    s = summarize(lat)
                    thr = bsize * len(lat) / (sum(lat) / 1e6)
                    self.summary.writemany([{"scale": self.scale, "scenario": f"S4_b{bsize}", "variant": v,
                                             "cache": "warm", **{x: round(y, 3) for x, y in s.items()}}])
                    print(f"  S4   {v:5} batch={bsize:<5} p50={s['p50_ms']:9.3f} ms/batch  {thr:9.0f} trips/s", flush=True)
            finally:
                repo.delete_trips([d["trip_number"] for d in docs])
        sink.close()

    # ---------- 5. конкурентне навантаження ----------
    def s8(self) -> None:
        print("[S8] змішане навантаження", flush=True)
        duration = 5 if self.quick else 20
        # S8 обмежений часом, тому варіанти встигають різну кількість записів. Щоб дані лишились
        # однаковими, події дописуються в тимчасові рейси: їх вставляємо перед S8 і видаляємо після.
        from lab2.generator import extra_trips
        tmp = extra_trips(2000, self.universe["max_trip_id"] + 1, seed=self.seed + 8)
        uni = {**self.universe, "by_status": {**self.universe["by_status"],
                                              "in_progress": [d["trip_number"] for d in tmp]}}
        for v in self.variants:
            self.settle()
            self.repos[v].insert_trips(tmp)
            try:
                for workers in (1, 8, 32):
                    res = concurrency.run(v, workers, duration, uni, self.seed)
                    concurrency.save(self.out, self.scale, v, workers, duration, res)
            finally:
                self.repos[v].delete_trips([d["trip_number"] for d in tmp])

    def close(self) -> None:
        self.close_sinks()
        for r in self.repos.values():
            r.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lab2.bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--scale", required=True)
    run.add_argument("--variants", default=",".join(VARIANTS))
    run.add_argument("--only", default="warm,server,cold,writes,s8")
    run.add_argument("--quick", action="store_true", help="мала кількість повторів — лише для налагодження")
    run.add_argument("--seed", type=int, default=2026)
    run.add_argument("--out", default=None, help="каталог результатів (за замовчуванням results/<scale>)")
    a = ap.parse_args(argv)

    b = Bench(a.scale, a.variants.split(","), a.quick, a.seed)
    if a.out:
        b.out = RESULTS / a.out
    stages = a.only.split(",")
    dump_json(b.out / "env.json", {"started": datetime.now(UTC), "args": vars(a), "env": environment_info(),
                                   "trips": len(b.universe["trip_numbers"])})
    t0 = time.time()
    try:
        b.verify()
        b.sizes()
        for st in stages:
            b.begin(st)
            {"warm": b.warm_reads, "server": b.server_side, "cold": b.cold, "writes": b.writes, "s8": b.s8}[st]()
        if any(st in stages for st in ("writes", "s8")):
            b.verify("_after")      # після записів варіанти мають лишитись еквівалентними
    finally:
        b.close()
    print(f"готово за {time.time() - t0:.0f} s; результати в {b.out}")


if __name__ == "__main__":
    main()
