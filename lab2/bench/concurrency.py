"""S8: змішане навантаження від 1, 8 і 32 клієнтів.

Клієнти це окремі процеси, кожен зі своїм підключенням (через GIL потоки не підходять).
Суміш: 60% S1, 20% S2, 15% S3a, 5% S7. Послідовність операцій клієнта залежить від seed
та номера клієнта, тому однакова для всіх варіантів.
"""
from __future__ import annotations

import multiprocessing as mp
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from lab2 import equivalence as eq
from lab2.bench.common import CsvSink

MIX = (("S1", 0.60), ("S2", 0.20), ("S3a", 0.15), ("S7", 0.05))
WARMUP_S = 3


def light_universe(u: dict, seed: int) -> dict:
    """Підмножина даних для передачі в процеси."""
    r = random.Random(f"{seed}:S8:universe")
    tn = u["trip_numbers"]
    ip = u["by_status"]["in_progress"]
    return {"trip_numbers": r.sample(tn, min(len(tn), 50_000)), "open": r.sample(ip, min(len(ip), 5_000)),
            "drivers": u["drivers"], "period_start": u["period_start"], "period_end": u["period_end"]}


def _worker(variant: str, idx: int, seed: int, uni: dict, start_at: float, dur_s: float, q, mix=MIX) -> None:
    from lab2.repos import open_repo
    repo = open_repo(variant)
    rng = random.Random(f"{seed}:S8:{idx}")
    ops, weights = zip(*mix)
    lat: dict[str, list[float]] = {o: [] for o in ops}
    errors: dict[str, int] = {o: 0 for o in ops}
    base_at = datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(days=idx)
    i = 0
    while time.time() < start_at:
        time.sleep(0.005)
    t_meas, t_end = start_at + WARMUP_S, start_at + WARMUP_S + dur_s
    while (now := time.time()) < t_end:
        op = rng.choices(ops, weights)[0]
        if op == "S3a":
            tn = rng.choice(uni["open"])
            ev = {"type": "checkpoint", "at": base_at + timedelta(seconds=i), "odometer": 600_000 + i,
                  "location": f"S8-{idx}"}
            fn = lambda: repo.append_event(tn, ev)
        else:
            kw = eq.READ_PARAMS[op](rng, uni)
            fn = lambda: eq.READ_CALLS[op](repo, kw)
        i += 1
        t0 = time.perf_counter_ns()
        try:
            fn()
        except Exception:
            errors[op] += 1
            continue
        us = (time.perf_counter_ns() - t0) / 1000
        if now >= t_meas:
            lat[op].append(us)
    repo.close()
    q.put((idx, lat, errors))


def run(variant: str, workers: int, dur_s: float, universe: dict, seed: int, mix=MIX) -> dict[str, Any]:
    uni = light_universe(universe, seed)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    start_at = time.time() + 3 + workers * 0.15        # час на старт процесів і підключень
    procs = [ctx.Process(target=_worker, args=(variant, i, seed, uni, start_at, dur_s, q, mix)) for i in range(workers)]
    for p in procs:
        p.start()
    results = [q.get(timeout=dur_s + WARMUP_S + 120) for _ in procs]
    for p in procs:
        p.join()
    lat: dict[str, list[float]] = {}
    errors: dict[str, int] = {}
    for _, l, e in results:
        for op, xs in l.items():
            lat.setdefault(op, []).extend(xs)
        for op, n in e.items():
            errors[op] = errors.get(op, 0) + n
    return {"lat": lat, "errors": errors}


def save(out: Path, scale: str, variant: str, workers: int, dur_s: float, res: dict,
         fname: str = "s8_throughput.csv") -> None:
    sink = CsvSink(out / fname, ["scale", "variant", "workers", "op", "count", "ops_per_s",
                                            "p50_ms", "p95_ms", "p99_ms", "errors"])
    all_lat = [x for xs in res["lat"].values() for x in xs]
    rows = []
    for op, xs in list(res["lat"].items()) + [("ALL", all_lat)]:
        a = np.asarray(xs) / 1000 if xs else np.asarray([np.nan])
        p50, p95, p99 = np.percentile(a, [50, 95, 99])
        rows.append({"scale": scale, "variant": variant, "workers": workers, "op": op, "count": len(xs),
                     "ops_per_s": round(len(xs) / dur_s, 1), "p50_ms": round(p50, 3), "p95_ms": round(p95, 3),
                     "p99_ms": round(p99, 3),
                     "errors": sum(res["errors"].values()) if op == "ALL" else res["errors"].get(op, 0)})
    sink.writemany(rows)
    sink.close()
    tot = rows[-1]
    print(f"  S8   {variant:5} workers={workers:<3} {tot['ops_per_s']:8.1f} ops/s  p50={tot['p50_ms']:8.3f}  "
          f"p99={tot['p99_ms']:8.3f} ms  errors={tot['errors']}", flush=True)
