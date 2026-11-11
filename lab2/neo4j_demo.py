"""Демо запитів Cypher: виконує neo4j/queries.cypher і друкує пояснення, запит та результат.

Запуск: uv run python -m lab2.neo4j_demo   (граф має бути завантажено: lab2.neo4j_load)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .db import neo4j_driver

QUERIES = Path(__file__).resolve().parent.parent / "neo4j" / "queries.cypher"
MAX_ROWS = 10


@dataclass
class Step:
    section: str
    comment: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    cypher: str = ""
    index: int = 1       # номер кроку в розділі
    total: int = 1       # кількість кроків у розділі


def parse(text: str) -> list[Step]:
    steps: list[Step] = []
    section, comment, params, body = "", [], {}, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("// ###"):
            section, comment, params, body = s[6:].strip(), [], {}, []
        elif s.startswith("// @param"):
            k, _, v = s[len("// @param"):].partition("=")
            v = v.strip()
            try:
                params[k.strip()] = json.loads(v)
            except ValueError:
                params[k.strip()] = v
        elif s.startswith("//"):
            comment.append(s[2:].strip())
        elif s:
            body.append(line.rstrip())
            if s.endswith(";"):
                steps.append(Step(section, comment, params, "\n".join(body).rstrip(";")))
                comment, params, body = [], {}, []
    for st in steps:
        same = [x for x in steps if x.section == st.section]
        st.total, st.index = len(same), same.index(st) + 1
    return steps


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.1f}"
    if isinstance(v, list):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    return str(v)


def table(rows: list[dict]) -> str:
    if not rows:
        return "(порожньо)"
    cols = list(rows[0])
    shown = rows[:MAX_ROWS]
    cells = [[fmt(r[c]) for c in cols] for r in shown]
    w = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
    out = ["  ".join(c.ljust(w[i]) for i, c in enumerate(cols)),
           "  ".join("-" * x for x in w)]
    out += ["  ".join(r[i].ljust(w[i]) for i in range(len(cols))) for r in cells]
    if len(rows) > MAX_ROWS:
        out.append(f"... ще {len(rows) - MAX_ROWS} рядків")
    return "\n".join(out)


def main() -> None:
    steps = parse(QUERIES.read_text(encoding="utf-8"))
    with neo4j_driver() as drv, drv.session() as s:
        # прибираємо залишки попереднього запуску, якщо він впав
        s.run("MATCH (n {demo: true}) DETACH DELETE n").consume()
        try:
            for st in steps:
                title = st.section + (f" (крок {st.index}/{st.total})" if st.total > 1 else "")
                print("=" * 78)
                print(title)
                print("-" * 78)
                if st.comment:
                    print("Пояснення: " + " ".join(st.comment))
                if st.params:
                    print("Параметри: " + ", ".join(f"{k}={v}" for k, v in st.params.items()))
                print("Cypher:")
                print("\n".join("  " + ln for ln in st.cypher.splitlines()))
                rows = s.run(st.cypher, st.params).data()
                print("Результат:")
                print("\n".join("  " + ln for ln in table(rows).splitlines()))
                print()
        finally:
            s.run("MATCH (n {demo: true}) DETACH DELETE n").consume()


if __name__ == "__main__":
    main()
