"""MongoDB (fleet_bench.trips): варіант `mongo`."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from bson.decimal128 import Decimal128

from .. import codec
from ..db import mongo_db
from .base import EXPLAINABLE, ServerPlan
from .sql import D2, money, year_bounds

S2_PROJECTION = {"planned_start": 1, "status": 1, "route.from": 1, "route.to": 1,
                 "customer.name": 1, "totals.freight_price": 1}
OPEN = ["planned", "in_progress"]


# $round у Mongo округлює до парного, а PG round(numeric) і генератор від нуля.
# liters*unit_price має рівно 4 знаки, тому 0.00001 підштовхує точні половинки (...50) вгору
# і не змінює решту значень.
_HALF_UP_MUL = {"$add": [{"$multiply": ["$ev.liters", "$ev.unit_price"]}, {"$literal": Decimal128("0.00001")}]}


def _s5a_pipeline(year: int) -> list[dict]:
    a, b = year_bounds(year)
    return [
        {"$match": {"planned_start": {"$gte": a, "$lt": b}, "status": "completed"}},
        {"$group": {"_id": {"c": "$customer.id", "m": {"$month": "$planned_start"}},
                    "n": {"$sum": 1}, "rev": {"$sum": "$totals.freight_price"}}},
        {"$sort": {"_id.c": 1, "_id.m": 1}},
    ]


def _s5b_pipeline(year: int) -> list[dict]:
    a, b = year_bounds(year)
    return [
        # документи, події яких можуть потрапити в рік. Генератор ставить події в межах від planned_start - 2 год
        # до planned_start + 7 днів, тому planned_start береться з [рік - 7 днів, рік + 1 + 2 год).
        {"$match": {"planned_start": {"$gte": a - timedelta(days=7), "$lt": b + timedelta(hours=2)}, "events.type": "fuel"}},
        {"$project": {"v": "$vehicle.id", "ev": {"$filter": {
            "input": "$events", "as": "e",
            "cond": {"$and": [{"$eq": ["$$e.type", "fuel"]}, {"$gte": ["$$e.at", a]}, {"$lt": ["$$e.at", b]}]}}}}},
        {"$unwind": "$ev"},
        {"$group": {"_id": "$v", "fills": {"$sum": 1}, "liters": {"$sum": "$ev.liters"},
                    "cost": {"$sum": {"$round": [_HALF_UP_MUL, 2]}}}},
        {"$sort": {"_id": 1}},
    ]


def _find_all(obj: Any, key: str, acc: list | None = None) -> list:
    """Усі значення за ключем key у плані."""
    acc = [] if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                acc.append(v)
            _find_all(v, key, acc)
    elif isinstance(obj, list):
        for x in obj:
            _find_all(x, key, acc)
    return acc


class MongoRepository:
    name = "mongo"

    def __init__(self) -> None:
        self.db = mongo_db()
        self.col = self.db["trips"]
        self.client = self.db.client

    def close(self) -> None:
        self.client.close()

    # ---- S1
    def get_trip(self, trip_number: str) -> dict | None:
        d = self.col.find_one({"_id": trip_number})
        return None if d is None else codec.from_mongo(d)

    # ---- S2
    def _s2_find(self, driver_id, date_from, date_to) -> dict:
        return {"driver.id": driver_id, "planned_start": {"$gte": date_from, "$lt": date_to}}

    def driver_trips(self, driver_id, date_from, date_to, page, page_size=20) -> list[tuple]:
        cur = (self.col.find(self._s2_find(driver_id, date_from, date_to), S2_PROJECTION)
               .sort([("planned_start", -1), ("_id", -1)]).skip(page * page_size).limit(page_size))
        return [(d["_id"], codec._fix(d["planned_start"]), d["status"], d["route"]["from"], d["route"]["to"],
                 d["customer"]["name"], codec._fix(d["totals"]["freight_price"]).quantize(D2)) for d in cur]

    # ---- S3
    @staticmethod
    def _s3_update(event: dict) -> dict:
        upd: dict = {"$push": {"events": event}}
        if event["type"] == "fuel":
            liters = event["liters"]
            upd["$inc"] = {"totals.fuel_l": Decimal128(liters),
                           "totals.fuel_cost": Decimal128(money(liters, event["unit_price"]))}
        return upd

    def append_event(self, trip_number: str, event: dict) -> None:
        # один update_one, щоб подія і підсумки змінювались разом
        r = self.col.update_one({"_id": trip_number}, self._s3_update(event))
        if r.matched_count == 0:
            raise LookupError(f"Рейс {trip_number} не знайдено")

    # ---- S4
    def insert_trips(self, docs: list[dict]) -> None:
        self.col.insert_many([codec.to_mongo(d) for d in docs], ordered=True)

    # ---- S5
    def revenue_by_customer_month(self, year: int) -> list[tuple]:
        return [(r["_id"]["c"], r["_id"]["m"], r["n"], codec._fix(r["rev"]).quantize(D2))
                for r in self.col.aggregate(_s5a_pipeline(year))]

    def fuel_by_vehicle(self, year: int) -> list[tuple]:
        return [(r["_id"], r["fills"], codec._fix(r["liters"]).quantize(D2), codec._fix(r["cost"]).quantize(D2))
                for r in self.col.aggregate(_s5b_pipeline(year))]

    # ---- S6
    @staticmethod
    def _s6_filter(customer_id: int, scope: str) -> dict:
        f: dict = {"customer.id": customer_id}
        if scope == "open":
            f["status"] = {"$in": OPEN}
        return f

    def rename_customer(self, customer_id: int, new_name: str, scope: str = "all") -> int:
        r = self.col.update_many(self._s6_filter(customer_id, scope), {"$set": {"customer.name": new_name}})
        return r.matched_count

    # ---- S7
    def _s7_find(self, adr_class, date_from, date_to) -> dict:
        return {"cargo.adr_class": adr_class, "planned_start": {"$gte": date_from, "$lt": date_to}}

    def adr_trips(self, adr_class: int, date_from: datetime, date_to: datetime) -> list[tuple]:
        cur = (self.col.find(self._s7_find(adr_class, date_from, date_to), {"planned_start": 1})
               .sort([("planned_start", 1), ("_id", 1)]))
        return [(d["_id"], codec._fix(d["planned_start"])) for d in cur]

    # ---- explain
    def explain(self, op: str, **kw: Any) -> ServerPlan:
        if op not in EXPLAINABLE:
            raise ValueError(f"explain не підтримує {op}")
        c = "trips"
        if op == "S1":
            cmd = {"find": c, "filter": {"_id": kw["trip_number"]}, "limit": 1}
        elif op == "S2":
            ps = kw.get("page_size", 20)
            cmd = {"find": c, "filter": self._s2_find(kw["driver_id"], kw["date_from"], kw["date_to"]),
                   "projection": S2_PROJECTION, "sort": {"planned_start": -1, "_id": -1},
                   "skip": kw["page"] * ps, "limit": ps}
        elif op == "S5a":
            cmd = {"aggregate": c, "pipeline": _s5a_pipeline(kw["year"]), "cursor": {}}
        elif op == "S5b":
            cmd = {"aggregate": c, "pipeline": _s5b_pipeline(kw["year"]), "cursor": {}}
        elif op == "S6":
            f = self._s6_filter(kw["customer_id"], kw.get("scope", "all"))
            cmd = {"update": c, "updates": [{"q": f, "u": {"$set": {"customer.name": kw["new_name"]}},
                                              "multi": True}]}
        else:
            cmd = {"find": c, "filter": self._s7_find(kw["adr_class"], kw["date_from"], kw["date_to"]),
                   "projection": {"planned_start": 1}, "sort": {"planned_start": 1, "_id": 1}}
        res = self.db.command("explain", cmd, verbosity="executionStats")
        return _server_plan(res)

    # ---- службові
    def delete_trips(self, trip_numbers: list[str]) -> int:
        return self.col.delete_many({"_id": {"$in": list(trip_numbers)}}).deleted_count

    def count(self) -> dict[str, int]:
        def n_type(t):
            return {"$size": {"$filter": {"input": "$events", "as": "e", "cond": {"$eq": ["$$e.type", t]}}}}
        r = list(self.col.aggregate([{"$group": {
            "_id": None, "trips": {"$sum": 1}, "cargo": {"$sum": {"$size": "$cargo"}},
            "events": {"$sum": {"$size": "$events"}}, "fuel": {"$sum": n_type("fuel")},
            "incidents": {"$sum": n_type("incident")}}}], allowDiskUse=True))
        r = r[0] if r else dict.fromkeys(("trips", "cargo", "events", "fuel", "incidents"), 0)
        return {k: r[k] for k in ("trips", "cargo", "events", "fuel", "incidents")}


def _server_plan(res: dict) -> ServerPlan:
    """Серверний час і ключові метрики з відповіді explain.

    server_ms: для find/update це executionStats.executionTimeMillis. Для aggregate береться максимум
    з нього і з executionTimeMillisEstimate усіх стадій (вони накопичувальні).
    """
    stats_all = _find_all(res, "executionStats")
    stats = stats_all[0] if stats_all else {}
    times = [float(t) for t in _find_all(res, "executionTimeMillis")]
    times += [float(t) for t in _find_all(res, "executionTimeMillisEstimate")]
    wins = _find_all(res, "winningPlan")
    win = wins[0] if wins else {}
    idx = _find_all(win, "indexName")
    notes = {
        "totalKeysExamined": stats.get("totalKeysExamined"),
        "totalDocsExamined": stats.get("totalDocsExamined"),
        "nReturned": stats.get("nReturned"),
        "index": idx[0] if idx else None,
        "stage": (win.get("stage") if isinstance(win, dict) else None),
        "stages": sorted({s for s in _find_all(win, "stage") if isinstance(s, str)}),
    }
    return ServerPlan(server_ms=max(times) if times else 0.0, plan=res, notes=notes)
