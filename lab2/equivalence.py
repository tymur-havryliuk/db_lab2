"""Перевірка, що всі варіанти зберігання повертають однакові результати.

Використовується в tests/test_equivalence.py і в бенчмарку перед прогоном (тільки читання).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from lab2.repos.base import TripRepository

UTC = timezone.utc


# ---------- нормалізація ----------

def normalize(v: Any) -> Any:
    """Зводить значення до форми, яку можна порівнювати.

    Decimal('540.0') і Decimal('540') рівні, datetime переводиться в UTC, tuple рівний list.
    float не очікується, тому його не округлюємо, а позначаємо.
    """
    if isinstance(v, bool):
        return ("bool", v)          # у Python True == 1, тому bool позначаємо явно
    if v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, Decimal):
        return ("dec", format(v.normalize(), "f"))
    if isinstance(v, float):
        return ("FLOAT!", repr(v))
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return ("NAIVE-DT!", v.isoformat())
        return ("dt", v.astimezone(UTC).isoformat())
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [normalize(x) for x in v]
    return ("?", type(v).__name__, repr(v))


def diff(a: Any, b: Any, path: str = "$", out: list[str] | None = None, limit: int = 10) -> list[str]:
    """Список розбіжностей між двома нормалізованими значеннями."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: відсутній ліворуч")
            elif k not in b:
                out.append(f"{path}.{k}: відсутній праворуч")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list) and not (a and isinstance(a[0], str) and a[0] in ("dec", "dt", "bool")):
        if len(a) != len(b):
            out.append(f"{path}: довжина {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


# ---------- параметри ----------

@dataclass
class Params:
    """Параметри запитів, однакові для всіх варіантів (фіксований seed)."""
    trip_numbers: list[str]
    open_trip_numbers: list[str]          # in_progress, для S3
    driver_ids: list[int]
    customer_ids: list[int]
    years: list[int]
    period_start: datetime
    period_end: datetime
    extra: dict[str, Any] = field(default_factory=dict)


def load_universe(sql_repo: TripRepository) -> dict[str, Any]:
    """Читає з нормалізованої бази значення, з яких вибираються параметри."""
    conn = sql_repo.conn  # type: ignore[attr-defined]
    with conn.cursor() as cur:
        cur.execute("SELECT trip_number, status::text FROM bench.trips ORDER BY trip_id")
        rows = cur.fetchall()
        cur.execute("SELECT driver_id FROM bench.drivers ORDER BY 1")
        drivers = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT customer_id FROM bench.customers ORDER BY 1")
        customers = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT min(planned_start), max(planned_start), max(trip_id) FROM bench.trips")
        pmin, pmax, max_id = cur.fetchone()
    conn.rollback()
    return {
        "trip_numbers": [r[0] for r in rows],
        "by_status": {s: [r[0] for r in rows if r[1] == s] for s in ("planned", "in_progress", "completed", "cancelled")},
        "drivers": drivers, "customers": customers,
        "period_start": pmin, "period_end": pmax, "max_trip_id": max_id,
    }


def random_window(rng: random.Random, start: datetime, end: datetime, days: int) -> tuple[datetime, datetime]:
    span = (end - start).total_seconds() - days * 86400
    a = start + timedelta(seconds=rng.uniform(0, max(span, 0)))
    a = a.replace(hour=0, minute=0, second=0, microsecond=0)
    return a, a + timedelta(days=days)


# Генератори параметрів для кожної операції: (rng, universe) -> kwargs.
def p_s1(rng, u):
    return {"trip_number": rng.choice(u["trip_numbers"])}


def p_s2(rng, u):
    a, b = random_window(rng, u["period_start"], u["period_end"], 365)
    return {"driver_id": rng.choice(u["drivers"]), "date_from": a, "date_to": b, "page": rng.randint(0, 2)}


def p_s5a(rng, u):
    return {"year": rng.randint(u["period_start"].year, u["period_end"].year - 1)}


p_s5b = p_s5a


def p_s7(rng, u):
    a, b = random_window(rng, u["period_start"], u["period_end"], 30)
    return {"adr_class": 3, "date_from": a, "date_to": b}


READ_PARAMS: dict[str, Callable] = {"S1": p_s1, "S2": p_s2, "S5a": p_s5a, "S5b": p_s5b, "S7": p_s7}

READ_CALLS: dict[str, Callable[[TripRepository, dict], Any]] = {
    "S1": lambda r, kw: r.get_trip(**kw),
    "S2": lambda r, kw: r.driver_trips(**kw),
    "S5a": lambda r, kw: r.revenue_by_customer_month(**kw),
    "S5b": lambda r, kw: r.fuel_by_vehicle(**kw),
    "S7": lambda r, kw: r.adr_trips(**kw),
}


# ---------- перевірки ----------

@dataclass
class Report:
    checked: int = 0
    mismatches: list[str] = field(default_factory=list)
    empty_results: dict[str, int] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.mismatches.append(msg)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def compare(repos: dict[str, TripRepository], op: str, kw: dict, call: Callable, rep: Report) -> Any:
    """Викликає операцію в усіх варіантах і порівнює з першим (еталон це SQL)."""
    names = list(repos)
    results = {n: normalize(call(repos[n], kw)) for n in names}
    ref = results[names[0]]
    rep.checked += 1
    if ref in (None, []):
        rep.empty_results[op] = rep.empty_results.get(op, 0) + 1
    for n in names[1:]:
        d = diff(ref, results[n])
        if d:
            rep.fail(f"{op} {kw}: {names[0]} vs {n}: " + "; ".join(d))
    return ref


def check_reads(repos: dict[str, TripRepository], universe: dict, seed: int = 7, n: int = 20) -> Report:
    """Перевірка читання: S1 (усі статуси і неіснуючий номер), S2, S5a, S5b, S7 і кількості."""
    rep = Report()
    rng = random.Random(seed)

    counts = {k: normalize(r.count()) for k, r in repos.items()}
    first = next(iter(counts))
    for k, c in counts.items():
        for d in diff(counts[first], c):
            rep.fail(f"count: {first} vs {k}: {d}")

    # S1: по кілька рейсів кожного статусу, щоб покрити події, null-поля і cancel_reason
    s1 = [rng.choice(lst) for lst in universe["by_status"].values() if lst for _ in range(3)]
    s1 += [rng.choice(universe["trip_numbers"]) for _ in range(n)]
    s1.append("TR-1999-000001")   # неіснуючий, очікуємо None скрізь
    for tn in s1:
        compare(repos, "S1", {"trip_number": tn}, READ_CALLS["S1"], rep)

    for op in ("S2", "S7"):
        for _ in range(n):
            compare(repos, op, READ_PARAMS[op](rng, universe), READ_CALLS[op], rep)
    # S7 з рідкісними класами, щоб були майже порожні результати
    for cls in (1, 2, 8):
        a, b = random_window(rng, universe["period_start"], universe["period_end"], 180)
        compare(repos, "S7", {"adr_class": cls, "date_from": a, "date_to": b}, READ_CALLS["S7"], rep)
    # усі роки періоду: S5 дешевий, і це потрібно для порогу checked >= 150 у тесті
    years = range(universe["period_start"].year, universe["period_end"].year + 1)
    for y in years:
        for op in ("S5a", "S5b"):
            compare(repos, op, {"year": y}, READ_CALLS[op], rep)
    return rep


def check_writes(repos: dict[str, TripRepository], universe: dict, new_docs: list[dict]) -> Report:
    """Перевірка записів (S3, S4, S6). Дані змінюються, але в кінці база повертається як була.

    new_docs: нові рейси з trip_id > max_trip_id. Їх вставляємо (S4), дописуємо події (S3),
    порівнюємо S1 і видаляємо.
    """
    rep = Report()
    names = [d["trip_number"] for d in new_docs]
    try:
        for r in repos.values():
            r.insert_trips(new_docs)
        for tn in names:
            compare(repos, "S4→S1", {"trip_number": tn}, READ_CALLS["S1"], rep)

        base = max(d["planned_start"] for d in new_docs) + timedelta(days=1)
        ev_checkpoint = {"type": "checkpoint", "at": base, "odometer": 999_001, "location": "Тест-пункт"}
        ev_fuel = {"type": "fuel", "at": base + timedelta(minutes=5), "odometer": 999_010,
                   "liters": Decimal("123.45"), "unit_price": Decimal("51.37"), "station": "Тест АЗС"}
        for tn in names[:3]:
            for ev in (ev_checkpoint, ev_fuel):
                for r in repos.values():
                    r.append_event(tn, dict(ev))
            ref = compare(repos, "S3→S1", {"trip_number": tn}, READ_CALLS["S1"], rep)
            if ref is not None and [e["type"] for e in ref["events"][-2:]] != ["checkpoint", "fuel"]:
                rep.fail(f"S3: події не дописані в кінець для {tn}")

        # S6 scope=all: після перейменування всі варіанти мають нову назву
        cust = next((d for d in new_docs if d["status"] == "completed"), new_docs[0])["customer"]
        cid, old = cust["id"], cust["name"]
        try:
            for r in repos.values():
                r.rename_customer(cid, old + " (перейменовано)", "all")
            for tn in names[:3]:
                compare(repos, "S6all→S1", {"trip_number": tn}, READ_CALLS["S1"], rep)
            # scope=open: у документах змінюються тільки відкриті рейси, а в SQL назва завжди одна.
            for r in repos.values():
                r.rename_customer(cid, old + " (open)", "open")
            completed = [d["trip_number"] for d in new_docs if d["customer"]["id"] == cid and d["status"] == "completed"]
            for tn in completed[:1]:
                sql_name = repos["sql"].get_trip(tn)["customer"]["name"] if "sql" in repos else None
                for n, r in repos.items():
                    if n == "sql":
                        continue
                    got = r.get_trip(tn)["customer"]["name"]
                    if got != old + " (перейменовано)":
                        rep.fail(f"S6open: {n} змінив знімок завершеного рейсу {tn}: {got!r}")
                if sql_name is not None and sql_name != old + " (open)":
                    rep.fail(f"S6open: sql показує {sql_name!r}")
                rep.checked += 1
        finally:
            for r in repos.values():
                r.rename_customer(cid, old, "all")
    finally:
        for r in repos.values():
            r.delete_trips(names)
    for tn in names[:2]:
        compare(repos, "cleanup→S1", {"trip_number": tn}, READ_CALLS["S1"], rep)
    return rep
