"""Фабрики підключень до PostgreSQL, MongoDB і Neo4j."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal

import psycopg
from bson.codec_options import CodecOptions, TypeCodec, TypeRegistry
from bson.decimal128 import Decimal128
from neo4j import Driver, GraphDatabase
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.write_concern import WriteConcern

from .config import settings


def pg_connect(autocommit: bool = False) -> psycopg.Connection:
    s = settings()
    return psycopg.connect(
        host=s.pg_host, port=s.pg_port, user=s.pg_user, password=s.pg_password,
        dbname=s.pg_db, options="-c timezone=UTC", application_name="lab2",
        autocommit=autocommit,
    )


class DecimalCodec(TypeCodec):
    """Decimal <-> Decimal128; при читанні Decimal128 повертається як Decimal."""
    python_type = Decimal
    bson_type = Decimal128

    def transform_python(self, value: Decimal) -> Decimal128:
        return Decimal128(value)

    def transform_bson(self, value: Decimal128) -> Decimal:
        return value.to_decimal()


def _codec_options() -> CodecOptions:
    return CodecOptions(
        tz_aware=True, tzinfo=timezone.utc,
        type_registry=TypeRegistry([DecimalCodec()]),
    )


def mongo_client() -> MongoClient:
    s = settings()
    return MongoClient(
        host=s.mongo_host, port=s.mongo_port,
        username=s.mongo_user, password=s.mongo_password,
        authSource="admin", tz_aware=True, tzinfo=timezone.utc,
    )


def mongo_db() -> Database:
    return mongo_client().get_database(
        settings().mongo_db,
        codec_options=_codec_options(),
        write_concern=WriteConcern(w=1, j=True),
    )


def mongo_trips() -> Collection:
    return mongo_db()["trips"]


def neo4j_driver() -> Driver:
    s = settings()
    return GraphDatabase.driver(
        f"bolt://{s.neo4j_host}:{s.neo4j_bolt_port}", auth=(s.neo4j_user, s.neo4j_password)
    )
