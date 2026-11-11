"""Спільний інтерфейс репозиторіїв (SQL, JSONB, Mongo).

Усі варіанти мають повертати однакові значення на однакових параметрах (tests/test_equivalence.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

Scope = Literal["all", "open"]

# Операції, для яких є explain.
EXPLAINABLE = ("S1", "S2", "S5a", "S5b", "S6", "S7")


@dataclass
class ServerPlan:
    """Результат explain: серверний час (мс) і сирий план."""
    server_ms: float
    plan: Any                     # JSON-план PG або executionStats Mongo (або список таких)
    notes: dict[str, Any] = field(default_factory=dict)   # напр. docs_examined, shared_hit/read


class TripRepository(Protocol):
    name: str                     # "sql" | "jsonb" | "mongo"

    # S1
    def get_trip(self, trip_number: str) -> dict | None: ...

    # S2
    def driver_trips(self, driver_id: int, date_from: datetime, date_to: datetime,
                     page: int, page_size: int = 20) -> list[tuple]: ...

    # S3
    def append_event(self, trip_number: str, event: dict) -> None: ...

    # S4
    def insert_trips(self, docs: list[dict]) -> None: ...

    # S5
    def revenue_by_customer_month(self, year: int) -> list[tuple]: ...
    def fuel_by_vehicle(self, year: int) -> list[tuple]: ...

    # S6
    def rename_customer(self, customer_id: int, new_name: str, scope: Scope = "all") -> int: ...

    # S7
    def adr_trips(self, adr_class: int, date_from: datetime, date_to: datetime) -> list[tuple]: ...

    # Виконує операцію `op` (одну з EXPLAINABLE) з тими самими kwargs, що й метод, через
    # EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) або explain(verbosity="executionStats").
    # S6 не має лишати змін: у PG це транзакція з ROLLBACK, а Mongo explain запис не застосовує.
    def explain(self, op: str, **kwargs: Any) -> ServerPlan: ...

    # службові
    def delete_trips(self, trip_numbers: list[str]) -> int: ...
    def count(self) -> dict[str, int]: ...
    def close(self) -> None: ...
