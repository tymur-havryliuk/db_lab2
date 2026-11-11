"""Створення і скидання схем PostgreSQL і MongoDB.

Запуск: uv run python -m lab2.schema {reset,indexes,drop-indexes,validator}
(validator тільки для Mongo)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import ROOT
from .db import mongo_db, pg_connect

SQL_DIR = ROOT / "sql"
MONGO_DIR = ROOT / "mongo"


def _run_sql(filename: str) -> None:
    sql = (SQL_DIR / filename).read_text(encoding="utf-8")
    with pg_connect(autocommit=True) as conn:
        conn.execute(sql)


def reset_sql() -> None:
    _run_sql("01_schema.sql")
    _run_sql("04_functions.sql")


def create_sql_indexes() -> None:
    _run_sql("02_indexes.sql")


def drop_sql_indexes() -> None:
    _run_sql("03_drop_indexes.sql")


def _index_specs() -> list[dict]:
    return json.loads((MONGO_DIR / "indexes.json").read_text(encoding="utf-8"))


def _mongo_validator() -> dict:
    return json.loads((MONGO_DIR / "validator.json").read_text(encoding="utf-8"))


def apply_mongo_validator() -> None:
    """Застосовує validator з файлу до наявної колекції trips (collMod)."""
    mongo_db().command(
        "collMod",
        "trips",
        validator=_mongo_validator(),
        validationLevel="strict",
        validationAction="error",
    )


def reset_mongo() -> None:
    db = mongo_db()
    db.drop_collection("trips")
    validator = _mongo_validator()
    db.create_collection(
        "trips", validator=validator, validationLevel="strict", validationAction="error"
    )


def create_mongo_indexes() -> None:
    from pymongo import IndexModel

    models = []
    for spec in _index_specs():
        spec = dict(spec)
        key = list(spec.pop("key").items())
        models.append(IndexModel(key, **spec))
    mongo_db()["trips"].create_indexes(models)


def drop_mongo_indexes() -> None:
    coll = mongo_db()["trips"]
    for spec in _index_specs():
        try:
            coll.drop_index(spec["name"])
        except Exception:  # індексу може не бути
            pass


def main(argv: list[str]) -> int:
    actions = {
        "reset": (reset_sql, reset_mongo),
        "indexes": (create_sql_indexes, create_mongo_indexes),
        "drop-indexes": (drop_sql_indexes, drop_mongo_indexes),
        "validator": (apply_mongo_validator,),
    }
    if len(argv) != 2 or argv[1] not in actions:
        print("Використання: python -m lab2.schema {reset,indexes,drop-indexes,validator}", file=sys.stderr)
        return 2
    for fn in actions[argv[1]]:
        fn()
        print(f"{argv[1]}: {fn.__name__} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
