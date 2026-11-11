"""Репозиторії для трьох варіантів зберігання. Інтерфейс: base.TripRepository."""
from __future__ import annotations

from .base import EXPLAINABLE, ServerPlan, TripRepository

VARIANTS = ("sql", "jsonb", "mongo")


def open_repo(name: str) -> TripRepository:
    """Створює репозиторій з новим підключенням."""
    if name == "sql":
        from .sql import SqlRepository
        return SqlRepository()
    if name == "jsonb":
        from .jsonb import JsonbRepository
        return JsonbRepository()
    if name == "mongo":
        from .mongo import MongoRepository
        return MongoRepository()
    raise ValueError(f"Невідомий варіант {name!r}; очікується один із {VARIANTS}")


__all__ = ["EXPLAINABLE", "ServerPlan", "TripRepository", "VARIANTS", "open_repo"]
