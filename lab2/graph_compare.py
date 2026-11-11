"""Порівняння Cypher (Neo4j) і SQL (WITH RECURSIVE) для графових задач, плюс PROFILE.

Запуск: uv run python -m lab2.graph_compare (потрібні Neo4j з графом на 10k рейсів і PostgreSQL)
Для кожної пари перевіряє збіг результатів, міряє медіану часу (20 повторів після 3 прогрівних)
і рахує рядки коду. Пише results/graph_compare.csv і results/neo4j_profile_*.txt.
"""
from __future__ import annotations

import csv
import statistics
import time
from pathlib import Path

from .config import ROOT
from .db import neo4j_driver, pg_connect
from .neo4j_demo import QUERIES, parse

SQL_FILE = ROOT / "neo4j" / "sql_equivalents.sql"
OUT = ROOT / "results"
WARMUP, REPEATS = 3, 20


def parse_sql(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    key = ""
    for ln in text.splitlines():
        if ln.startswith("-- ###"):
            key = ln[6:].strip()
            out[key] = []
        elif key:
            out[key].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def code_lines(q: str) -> int:
    return sum(1 for ln in q.splitlines() if ln.strip() and not ln.strip().startswith(("--", "//")))


def median_ms(fn) -> float:
    for _ in range(WARMUP):
        fn()
    ts = []
    for _ in range(REPEATS):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


def r1(x) -> float:
    return round(float(x), 1)


# нормалізатори: рядки результату Cypher або SQL перетворюють на ключ для порівняння
def n_hops_c(rows): return rows[0]["переїздів"]
def n_hops_s(rows): return rows[0][0]
def n_w_c(rows): return (r1(rows[0]["км_разом"]), tuple(rows[0]["маршрут"]))
def n_w_s(rows): return (r1(rows[0][1]), tuple(rows[0][2]))
def n_reach_c(rows): return [(r["переїздів"], r["міст"]) for r in rows]
def n_reach_s(rows): return [(r[0], r[1]) for r in rows]
def n_shared_c(rows): return [(r["колега"], r["спільних_замовників"]) for r in rows]
def n_shared_s(rows): return [(r[0], r[1]) for r in rows]
def n_top_c(rows): return [(r["id"], r["рейсів"], r1(r["вантажоперевезень_грн"])) for r in rows]
def n_top_s(rows): return [(r[0], r[2], r1(r[3])) for r in rows]

# ключ, розділ Cypher, параметри, назва, нормалізатори
CASES = [
    ("hops", "Найкоротший шлях за кількістю переїздів", {"from": "Київ", "to": "Одеса"},
     "Шлях за переїздами Київ→Одеса", n_hops_c, n_hops_s),
    ("weighted", "Найкоротший шлях за кілометрами (власний Cypher, без GDS/APOC)",
     {"from": "Київ", "to": "Одеса"}, "Зважений шлях за км Київ→Одеса (≤6)", n_w_c, n_w_s),
    ("reachable", "Досяжні міста за ≤ 2 переїздів", {"from": "Ужгород"},
     "Досяжні за ≤2 переїзди з Ужгорода", n_reach_c, n_reach_s),
    ("shared_drivers", "Водії зі спільними замовниками", {}, "Водії зі спільними замовниками",
     n_shared_c, n_shared_s),
    ("top_customers", "Топ замовників за кількістю рейсів", {}, "Топ замовників", n_top_c, n_top_s),
]


def fmt_profile(p: dict, depth: int = 0, lines: list | None = None) -> list[str]:
    lines = [] if lines is None else lines
    a = p.get("args", {})
    lines.append(f"{'  ' * depth}{p['operatorType']:<{34 - 2 * depth}} rows={a.get('Rows', '-'):>8} "
                 f"db_hits={a.get('DbHits', '-'):>9} page_cache_hits={a.get('PageCacheHits', '-'):>7} "
                 f"details={a.get('Details', '')}")
    for c in p.get("children", []):
        fmt_profile(c, depth + 1, lines)
    return lines


def save_profile(session, name: str, cypher: str, params: dict) -> None:
    res = session.run("PROFILE " + cypher, params)
    summ = res.consume()
    body = [f"PROFILE {name}", f"parameters: {params}", "", cypher, "",
            f"result_available_after_ms={summ.result_available_after}  "
            f"result_consumed_after_ms={summ.result_consumed_after}", "",
            *fmt_profile(summ.profile)]
    (OUT / f"neo4j_profile_{name}.txt").write_text("\n".join(body) + "\n", encoding="utf-8")


def main() -> None:
    cy = {s.section.split(" (крок")[0]: s for s in parse(QUERIES.read_text(encoding="utf-8"))}
    sq = parse_sql(SQL_FILE.read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    rows_out = []
    with neo4j_driver() as drv, drv.session() as ns, pg_connect(autocommit=True) as pg:
        pg.read_only = True
        cur = pg.cursor()
        for key, section, params, title, nc, nsq in CASES:
            c = cy[section]
            p = {**c.params, **params}
            run_c = lambda: ns.run(c.cypher, p).data()
            def run_s(q=sq[key], p=p):
                cur.execute(q, p)
                return cur.fetchall()
            rc, rs = run_c(), run_s()
            same = nc(rc) == nsq(rs)
            if not same:
                print(f"  ! розбіжність {key}:\n    cypher={nc(rc)}\n    sql   ={nsq(rs)}")
            tc, ts = median_ms(run_c), median_ms(run_s)
            rows_out.append({"query": title, "cypher_ms": round(tc, 2), "sql_ms": round(ts, 2),
                             "cypher_lines": code_lines(c.cypher), "sql_lines": code_lines(sq[key]),
                             "results_match": same})
            print(f"{title:<40} Cypher {tc:8.2f} мс | SQL {ts:9.2f} мс | рядків {code_lines(c.cypher)}/"
                  f"{code_lines(sq[key])} | збіг: {same}")
        for key, name in (("weighted", "weighted"), ("shared_drivers", "shared_drivers")):
            sec = next(x for k, s, *_ in CASES if k == key for x in [s])
            c = cy[sec]
            save_profile(ns, name, c.cypher, {**c.params, **next(x[2] for x in CASES if x[0] == key)})
    with open(OUT / "graph_compare.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0]))
        w.writeheader()
        w.writerows(rows_out)
    print(f"Записано {OUT / 'graph_compare.csv'} і профілі")


if __name__ == "__main__":
    main()
