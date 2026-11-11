"""Тест: SQL, JSONB і Mongo повертають однакові результати.

Потрібні завантажені дані (будь-який масштаб): uv run python -m lab2.load --scale 10k
Запуск: uv run pytest -q
"""
from decimal import Decimal
from datetime import datetime, timezone

import pytest

from lab2 import equivalence as eq

VARIANTS = ("sql", "jsonb", "mongo")


@pytest.fixture(scope="module")
def repos():
    from lab2.repos import open_repo
    rs = {v: open_repo(v) for v in VARIANTS}
    yield rs
    for r in rs.values():
        r.close()


@pytest.fixture(scope="module")
def universe(repos):
    return eq.load_universe(repos["sql"])


# --- компаратор має ловити розбіжності ---

def test_normalize_equal_decimals_and_tuples():
    assert eq.normalize((Decimal("540.0"), 1)) == eq.normalize([Decimal("540"), 1])


def test_diff_detects_value_key_order_and_float():
    a = eq.normalize({"x": [1, 2], "d": Decimal("1.00")})
    assert eq.diff(a, eq.normalize({"x": [2, 1], "d": Decimal("1.00")}))          # порядок у списку важливий
    assert eq.diff(a, eq.normalize({"x": [1, 2]}))                                  # відсутній ключ
    assert eq.diff(a, eq.normalize({"x": [1, 2], "d": 1.0}))                        # float замість Decimal
    assert eq.diff(eq.normalize({"f": True}), eq.normalize({"f": 1}))                # bool != int
    assert eq.diff(eq.normalize(datetime(2024, 1, 1, tzinfo=timezone.utc)),
                   eq.normalize(datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)))


# --- еквівалентність на реальних даних ---

def test_counts_match(repos):
    counts = {v: r.count() for v, r in repos.items()}
    assert counts["sql"]["trips"] > 0
    assert counts["sql"] == counts["jsonb"] == counts["mongo"]


def test_reads_equivalent(repos, universe):
    rep = eq.check_reads(repos, universe, seed=11, n=40)
    assert rep.ok, "\n".join(rep.mismatches[:20])
    assert rep.checked >= 150
    # більшість результатів непорожні
    assert sum(rep.empty_results.values()) < rep.checked / 2, rep.empty_results


def test_writes_equivalent_and_cleanup(repos, universe):
    from lab2.generator import extra_trips
    before = repos["sql"].count()
    docs = extra_trips(30, universe["max_trip_id"] + 1, seed=99)
    rep = eq.check_writes(repos, universe, docs)
    assert rep.ok, "\n".join(rep.mismatches[:20])
    for v, r in repos.items():
        assert r.count() == before, f"{v}: дані не повернулись до початкового стану"
