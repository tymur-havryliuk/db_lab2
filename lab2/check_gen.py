"""Перевірка генератора: детермінізм, кодеки туди й назад, формати довідників.

CLI: uv run python -m lab2.check_gen [--n 2000]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys

from .codec import from_jsonb, from_mongo, to_jsonb, to_mongo
from .generator import build_refs, extra_trips, generate_trips


def digest(n: int, seed: int = 42) -> str:
    h = hashlib.sha256()
    for d in generate_trips(n, seed):
        h.update(to_jsonb(d).encode())
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    n = ap.parse_args(argv).n
    h1, h2 = digest(n), digest(n)
    print(f"sha256 #1 {h1[:16]}…  #2 {h2[:16]}…  детермінізм: {'OK' if h1 == h2 else 'ПОМИЛКА'}")
    ok = h1 == h2
    print("інший seed відрізняється:", digest(200, 7) != digest(200, 42))

    docs = list(generate_trips(n)) + extra_trips(50, n + 1, 42)
    rt_ok = all(from_jsonb(to_jsonb(d)) == d and from_mongo(to_mongo(d)) == d for d in docs)
    print("round-trip jsonb/mongo (in-memory):", "OK" if rt_ok else "ПОМИЛКА")
    ok &= rt_ok

    refs = build_refs()
    rx = {
        "code": (r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+)*$", [r["code"] for r in refs.routes]),
        "plate": (r"^[ABCEHIKMOPTX]{2}[0-9]{4}[ABCEHIKMOPTX]{2}$", [v["plate"] for v in refs.vehicles]),
        "vin": (r"^[A-HJ-NPR-Z0-9]{17}$", [v["vin"] for v in refs.vehicles]),
        "license": (r"^[A-Z]{3}[0-9]{6}$", [d["license"] for d in refs.drivers]),
        "phone": (r"^\+380[0-9]{9}$", [d["phone"] for d in refs.drivers]),
        "tax": (r"^[0-9]{8}$", [c["tax_code"] for c in refs.customers]),
    }
    for name, (pat, vals) in rx.items():
        good = all(re.match(pat, v) for v in vals) and len(set(vals)) == len(vals)
        print(f"довідник {name}: {'OK' if good else 'ПОМИЛКА'} ({len(vals)})")
        ok &= good
    print("trip_number:", all(re.match(r"^TR-\d{4}-\d{6}$", d["trip_number"]) for d in docs))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
