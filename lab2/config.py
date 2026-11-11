"""Налаштування підключень з .env у корені репозиторію."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    pg_user: str
    pg_password: str
    pg_db: str
    pg_host: str
    pg_port: int
    mongo_user: str
    mongo_password: str
    mongo_db: str
    mongo_host: str
    mongo_port: int
    neo4j_user: str
    neo4j_password: str
    neo4j_host: str
    neo4j_bolt_port: int
    neo4j_http_port: int


def settings() -> Settings:
    load_dotenv(ROOT / ".env")
    e = os.environ
    return Settings(
        pg_user=e["PG_USER"], pg_password=e["PG_PASSWORD"], pg_db=e["PG_DB"],
        pg_host=e.get("PG_HOST", "localhost"), pg_port=int(e.get("PG_PORT", 5432)),
        mongo_user=e["MONGO_USER"], mongo_password=e["MONGO_PASSWORD"], mongo_db=e["MONGO_DB"],
        mongo_host=e.get("MONGO_HOST", "localhost"), mongo_port=int(e.get("MONGO_PORT", 27017)),
        neo4j_user=e["NEO4J_USER"], neo4j_password=e["NEO4J_PASSWORD"],
        neo4j_host=e.get("NEO4J_HOST", "localhost"),
        neo4j_bolt_port=int(e.get("NEO4J_BOLT_PORT", 7687)),
        neo4j_http_port=int(e.get("NEO4J_HTTP_PORT", 7474)),
    )
