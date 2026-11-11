"""Перетворення канонічного документа рейсу в Mongo, JSONB і рядки для SQL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

UTC = timezone.utc
_D1 = Decimal("0.1")
_D2 = Decimal("0.01")

# Колонки для COPY, без generated-колонок distance_km і total_cost та identity-колонок fuel_tx_id і incident_id
SQL_COLUMNS: dict[str, tuple[str, ...]] = {
    "cities": ("city_id", "name", "oblast"),
    "routes": ("route_id", "code", "from_city_id", "to_city_id", "distance_km", "duration_hours"),
    "vehicles": ("vehicle_id", "vin", "plate_number", "model"),
    "drivers": ("driver_id", "last_name", "first_name", "middle_name", "license_number", "phone"),
    "customers": ("customer_id", "name", "tax_code", "city_id"),
    "trips": ("trip_id", "trip_number", "vehicle_id", "driver_id", "route_id", "customer_id", "status",
              "planned_start", "planned_end", "started_at", "finished_at", "odometer_start",
              "odometer_end", "freight_price", "cancel_reason"),
    "trip_cargo_items": ("trip_id", "line_no", "kind", "title", "weight_kg", "packages", "temp_min",
                         "temp_max", "un_number", "adr_class", "packing_group", "length_cm",
                         "width_cm", "height_cm"),
    "trip_events": ("trip_id", "seq", "type", "occurred_at", "odometer_km", "location", "duration_min",
                    "reason", "receiver_name"),
    "fuel_transactions": ("trip_id", "event_seq", "vehicle_id", "driver_id", "filled_at", "quantity",
                          "unit_price", "odometer_km", "station"),
    "incidents": ("trip_id", "event_seq", "vehicle_id", "driver_id", "type", "occurred_at",
                  "description", "amount", "driver_at_fault"),
}
TRIP_TABLES = ("trips", "trip_cargo_items", "trip_events", "fuel_transactions", "incidents")

_DATE_FIELDS = ("planned_start", "planned_end", "started_at", "finished_at")


# --------------------------------------------------------------------------- Mongo
def to_mongo(doc: dict) -> dict:
    """_id це trip_number, окремо trip_number не дублюється. Decimal у Decimal128 перетворює TypeRegistry."""
    d = {"_id": doc["trip_number"]}
    d.update((k, v) for k, v in doc.items() if k != "trip_number")
    return d


def _fix(v):
    """Decimal128 у Decimal, datetime без часового поясу в UTC."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)
    if isinstance(v, dict):
        return {k: _fix(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_fix(x) for x in v]
    to_dec = getattr(v, "to_decimal", None)  # bson.Decimal128
    if to_dec is not None:
        return to_dec()
    return v


def from_mongo(d: dict) -> dict:
    out = {"trip_number": d["_id"]}
    out.update((k, _fix(v)) for k, v in d.items() if k != "_id")
    return out


# --------------------------------------------------------------------------- JSONB
def _iso(dt: datetime) -> str:
    if dt.tzinfo is not UTC and (dt.tzinfo is None or dt.utcoffset() != UTC.utcoffset(None)):
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat()[:19] + "Z"


def _default(o):
    if isinstance(o, datetime):
        return _iso(o)
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Неможливо серіалізувати {type(o)}")


def to_jsonb(doc: dict) -> str:
    return json.dumps(doc, default=_default, ensure_ascii=False, separators=(",", ":"))


def _q(v, quant: Decimal):
    if v is None:
        return None
    if isinstance(v, float):
        v = repr(v)
    return Decimal(v).quantize(quant) if not isinstance(v, Decimal) else v.quantize(quant)


def _parse_dt(s):
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def from_jsonb(obj_or_str) -> dict:
    d = json.loads(obj_or_str, parse_float=Decimal) if isinstance(obj_or_str, (str, bytes)) else obj_or_str
    d = dict(d)
    for f in _DATE_FIELDS:
        d[f] = _parse_dt(d.get(f))
    d["route"] = {**d["route"], "km": _q(d["route"]["km"], _D1)}
    t = dict(d["totals"])
    for f in ("fuel_l", "fuel_cost", "freight_price"):
        t[f] = _q(t[f], _D2)
    d["totals"] = t
    events = []
    for e in d["events"]:
        e = dict(e)
        e["at"] = _parse_dt(e["at"])
        for f in ("liters", "unit_price", "amount"):
            if f in e:
                e[f] = _q(e[f], _D2)
        events.append(e)
    d["events"] = events
    d["cargo"] = [dict(c) for c in d["cargo"]]
    return d


# --------------------------------------------------------------------------- SQL
def to_sql_rows(doc: dict) -> dict[str, list[tuple]]:
    tid = doc["trip_id"]
    veh, drv, rt, cus = doc["vehicle"]["id"], doc["driver"]["id"], doc["route"]["id"], doc["customer"]["id"]
    events = doc["events"]

    odo_start = odo_end = None
    if events:
        odo_start = events[0].get("odometer")
        for e in reversed(events):
            if "odometer" in e:
                odo_end = e["odometer"]
                break

    trips = [(tid, doc["trip_number"], veh, drv, rt, cus, doc["status"], doc["planned_start"],
              doc["planned_end"], doc["started_at"], doc["finished_at"], odo_start, odo_end,
              doc["totals"]["freight_price"], doc["cancel_reason"])]

    cargo = []
    for i, c in enumerate(doc["cargo"], 1):
        g = c.get
        cargo.append((tid, i, c["kind"], c["title"], c["kg"], g("packages"), g("temp_min"), g("temp_max"),
                      g("un_number"), g("adr_class"), g("packing_group"), g("length_cm"),
                      g("width_cm"), g("height_cm")))

    ev_rows, fuel, inc = [], [], []
    for j, e in enumerate(events, 1):
        t, at = e["type"], e["at"]
        if t == "fuel":
            ev_rows.append((tid, j, t, at, e["odometer"], None, None, None, None))
            fuel.append((tid, j, veh, drv, at, e["liters"], e["unit_price"], e["odometer"], e["station"]))
        elif t == "incident":
            ev_rows.append((tid, j, t, at, None, None, None, None, None))
            inc.append((tid, j, veh, drv, e["incident_type"], at, e["description"], e["amount"],
                        e["at_fault"]))
        elif t == "idle":
            ev_rows.append((tid, j, t, at, None, None, e["minutes"], e["reason"], None))
        elif t == "checkpoint":
            ev_rows.append((tid, j, t, at, e["odometer"], e["location"], None, None, None))
        elif t == "delivered":
            ev_rows.append((tid, j, t, at, e["odometer"], None, None, None, e["receiver"]))
        else:  # departed
            ev_rows.append((tid, j, t, at, e["odometer"], None, None, None, None))

    return {"trips": trips, "trip_cargo_items": cargo, "trip_events": ev_rows,
            "fuel_transactions": fuel, "incidents": inc}


def refs_rows(refs) -> dict[str, list[tuple]]:
    return {
        "cities": [(c["id"], c["name"], c["oblast"]) for c in refs.cities],
        "routes": [(r["id"], r["code"], r["from_city_id"], r["to_city_id"], r["distance_km"],
                    r["duration_hours"]) for r in refs.routes],
        "vehicles": [(v["id"], v["vin"], v["plate"], v["model"]) for v in refs.vehicles],
        "drivers": [(d["id"], d["last_name"], d["first_name"], d["middle_name"], d["license"], d["phone"])
                    for d in refs.drivers],
        "customers": [(c["id"], c["name"], c["tax_code"], c["city_id"]) for c in refs.customers],
    }
