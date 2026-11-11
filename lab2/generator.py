"""Детермінований генератор довідників і документів рейсів (е-ТТН).

Faker потрібен тільки для довідників (build_refs). Рейси генеруються на чистому Python і numpy,
щоб 1M документів не рахувались занадто довго.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Iterator

import numpy as np

UTC = timezone.utc
REF_SEED = 42

N_ROUTES, N_VEHICLES, N_DRIVERS, N_CUSTOMERS = 400, 500, 800, 2000

# Період історії рейсів (UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
P0 = int((datetime(2020, 1, 1, tzinfo=UTC) - EPOCH).total_seconds() // 60)
P1 = int((datetime(2026, 10, 31, 23, 59, tzinfo=UTC) - EPOCH).total_seconds() // 60)
EX0 = int((datetime(2026, 11, 1, tzinfo=UTC) - EPOCH).total_seconds() // 60)
EX1 = int((datetime(2026, 12, 31, 23, 59, tzinfo=UTC) - EPOCH).total_seconds() // 60)

STATUSES = ("completed", "cancelled", "planned", "in_progress")
STATUS_P = (0.85, 0.05, 0.07, 0.03)

# (назва, область, код з 3 літер)
CITIES = [
    ("Київ", "Київ", "KYV"), ("Львів", "Львівська", "LVV"), ("Одеса", "Одеська", "ODS"),
    ("Харків", "Харківська", "KHA"), ("Дніпро", "Дніпропетровська", "DNP"),
    ("Запоріжжя", "Запорізька", "ZPR"), ("Вінниця", "Вінницька", "VIN"),
    ("Полтава", "Полтавська", "PLT"), ("Чернігів", "Чернігівська", "CHE"),
    ("Черкаси", "Черкаська", "CHK"), ("Суми", "Сумська", "SUM"),
    ("Житомир", "Житомирська", "ZHT"), ("Рівне", "Рівненська", "RIV"),
    ("Луцьк", "Волинська", "LUT"), ("Тернопіль", "Тернопільська", "TER"),
    ("Івано-Франківськ", "Івано-Франківська", "IFR"), ("Ужгород", "Закарпатська", "UZH"),
    ("Чернівці", "Чернівецька", "CHV"), ("Хмельницький", "Хмельницька", "KHM"),
    ("Миколаїв", "Миколаївська", "MYK"), ("Херсон", "Херсонська", "KHE"),
    ("Кропивницький", "Кіровоградська", "KRP"), ("Біла Церква", "Київська", "BCR"),
    ("Кременчук", "Полтавська", "KRE"), ("Кривий Ріг", "Дніпропетровська", "KRR"),
    ("Маріуполь", "Донецька", "MRP"), ("Мелітополь", "Запорізька", "MEL"),
    ("Бердянськ", "Запорізька", "BRD"), ("Мукачево", "Закарпатська", "MUK"),
    ("Дрогобич", "Львівська", "DRO"), ("Стрий", "Львівська", "STR"),
    ("Коломия", "Івано-Франківська", "KOL"), ("Кам'янець-Подільський", "Хмельницька", "KAM"),
    ("Умань", "Черкаська", "UMA"), ("Бровари", "Київська", "BRO"),
    ("Бориспіль", "Київська", "BOR"), ("Ірпінь", "Київська", "IRP"),
    ("Фастів", "Київська", "FAS"), ("Нікополь", "Дніпропетровська", "NIK"),
    ("Павлоград", "Дніпропетровська", "PAV"), ("Кам'янське", "Дніпропетровська", "KMS"),
    ("Лозова", "Харківська", "LOZ"), ("Ізюм", "Харківська", "IZY"),
    ("Конотоп", "Сумська", "KON"), ("Шостка", "Сумська", "SHO"),
    ("Ніжин", "Чернігівська", "NIZ"), ("Прилуки", "Чернігівська", "PRY"),
    ("Новоград-Волинський", "Житомирська", "NVV"), ("Бердичів", "Житомирська", "BER"),
    ("Коростень", "Житомирська", "KOR"), ("Здолбунів", "Рівненська", "ZDO"),
    ("Дубно", "Рівненська", "DUB"), ("Ковель", "Волинська", "KOV"),
    ("Червоноград", "Львівська", "CHR"), ("Борислав", "Львівська", "BSL"),
    ("Калуш", "Івано-Франківська", "KAL"), ("Болград", "Одеська", "BOL"),
    ("Ізмаїл", "Одеська", "IZM"), ("Первомайськ", "Миколаївська", "PER"),
    ("Южноукраїнськ", "Миколаївська", "YUZ"),
]
assert len(CITIES) == 60 and len({c[2] for c in CITIES}) == 60

# Приблизні координати (широта, довгота) для відстаней маршрутів.
CITY_COORDS = {
    "Київ": (50.45, 30.52), "Львів": (49.84, 24.03), "Одеса": (46.48, 30.73), "Харків": (49.99, 36.23),
    "Дніпро": (48.46, 35.05), "Запоріжжя": (47.84, 35.14), "Вінниця": (49.23, 28.47), "Полтава": (49.59, 34.55),
    "Чернігів": (51.49, 31.29), "Черкаси": (49.44, 32.06), "Суми": (50.91, 34.80), "Житомир": (50.25, 28.66),
    "Рівне": (50.62, 26.25), "Луцьк": (50.75, 25.33), "Тернопіль": (49.55, 25.59),
    "Івано-Франківськ": (48.92, 24.71), "Ужгород": (48.62, 22.29), "Чернівці": (48.29, 25.94),
    "Хмельницький": (49.42, 26.99), "Миколаїв": (46.98, 31.99), "Херсон": (46.64, 32.61),
    "Кропивницький": (48.51, 32.26), "Біла Церква": (49.80, 30.11), "Кременчук": (49.07, 33.42),
    "Кривий Ріг": (47.91, 33.39), "Маріуполь": (47.10, 37.55), "Мелітополь": (46.84, 35.37),
    "Бердянськ": (46.76, 36.79), "Мукачево": (48.44, 22.72), "Дрогобич": (49.35, 23.51), "Стрий": (49.26, 23.85),
    "Коломия": (48.53, 25.04), "Кам'янець-Подільський": (48.68, 26.58), "Умань": (48.75, 30.22),
    "Бровари": (50.51, 30.79), "Бориспіль": (50.35, 30.95), "Ірпінь": (50.52, 30.25), "Фастів": (50.08, 29.92),
    "Нікополь": (47.57, 34.40), "Павлоград": (48.53, 35.87), "Кам'янське": (48.51, 34.61), "Лозова": (48.89, 36.32),
    "Ізюм": (49.21, 37.26), "Конотоп": (51.24, 33.20), "Шостка": (51.86, 33.48), "Ніжин": (51.05, 31.89),
    "Прилуки": (50.59, 32.39), "Новоград-Волинський": (50.59, 27.62), "Бердичів": (49.90, 28.60),
    "Коростень": (50.95, 28.64), "Здолбунів": (50.51, 26.24), "Дубно": (50.42, 25.73), "Ковель": (51.21, 24.71),
    "Червоноград": (50.39, 24.23), "Борислав": (49.29, 23.42), "Калуш": (49.02, 24.37), "Болград": (45.68, 28.61),
    "Ізмаїл": (45.35, 28.84), "Первомайськ": (48.04, 30.85), "Южноукраїнськ": (47.82, 31.18),
}
assert set(CITY_COORDS) == {c[0] for c in CITIES}

VEHICLE_MODELS = [
    "MAN TGX", "MAN TGS", "DAF XF", "DAF CF", "Volvo FH", "Volvo FM", "Scania R450",
    "Scania S500", "Mercedes-Benz Actros", "Renault T", "Iveco S-Way", "Ford F-Max",
]
VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # без I, O, Q
PLATE_LETTERS = "ABCEHIKMOPTX"
COMPANY_PREFIXES = ["ТОВ", "ПП", "ФОП", "АТ", "ПрАТ"]


@dataclass
class Refs:
    cities: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    vehicles: list[dict] = field(default_factory=list)
    drivers: list[dict] = field(default_factory=list)
    customers: list[dict] = field(default_factory=list)


def _d1(tenths: int) -> Decimal:
    return Decimal(tenths).scaleb(-1)


def _d2(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


@lru_cache(maxsize=4)
def _build_refs(seed: int) -> Refs:
    from faker import Faker

    Faker.seed(seed)
    fake = Faker("uk_UA")
    fake.seed_instance(seed)
    r = random.Random(f"refs-{seed}")

    refs = Refs()
    for i, (name, oblast, _code) in enumerate(CITIES, 1):
        refs.cities.append({"id": i, "name": name, "oblast": oblast})

    # Відстань між містами: дуга великого кола, помножена на 1.25 (дороги не прямі).
    # Спочатку кожне місто з'єднується з 4 найближчими сусідами в обидва боки, це дорожня мережа.
    # Решту до 400 маршрутів добавляють випадкові далекі рейси.
    def road_km(a: int, b: int) -> float:
        (la1, lo1), (la2, lo2) = (map(math.radians, CITY_COORDS[CITIES[x][0]]) for x in (a, b))
        h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        return 2 * 6371 * math.asin(math.sqrt(h)) * 1.25

    pairs: set[tuple[int, int]] = set()
    for a in range(60):
        for b in sorted((x for x in range(60) if x != a), key=lambda x: road_km(a, x))[:4]:
            pairs.update({(a, b), (b, a)})
    while len(pairs) < N_ROUTES:
        a, b = r.randrange(60), r.randrange(60)
        if a != b:
            pairs.add((a, b))
    for i, (a, b) in enumerate(sorted(pairs, key=lambda p: (r.random(), p)), 1):
        tenths = max(100, round(road_km(a, b) * 10))
        km = _d1(tenths)
        refs.routes.append({
            "id": i, "code": f"{CITIES[a][2]}-{CITIES[b][2]}-01",
            "from_city_id": a + 1, "to_city_id": b + 1,
            "from": CITIES[a][0], "to": CITIES[b][0],
            "distance_km": km,
            "duration_hours": _d1(max(1, round(tenths / 65))),  # приблизно km/65 год
        })

    vins, plates = set(), set()
    while len(refs.vehicles) < N_VEHICLES:
        vin = "".join(r.choice(VIN_CHARS) for _ in range(17))
        plate = (r.choice(PLATE_LETTERS) + r.choice(PLATE_LETTERS)
                 + "".join(r.choice("0123456789") for _ in range(4))
                 + r.choice(PLATE_LETTERS) + r.choice(PLATE_LETTERS))
        if vin in vins or plate in plates:
            continue
        vins.add(vin)
        plates.add(plate)
        refs.vehicles.append({
            "id": len(refs.vehicles) + 1, "vin": vin, "plate": plate,
            "model": r.choice(VEHICLE_MODELS),
        })

    licenses = set()
    while len(refs.drivers) < N_DRIVERS:
        lic = ("".join(r.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3))
               + "".join(r.choice("0123456789") for _ in range(6)))
        if lic in licenses:
            continue
        licenses.add(lic)
        male = r.random() < 0.9
        sx = "male" if male else "female"
        last = getattr(fake, f"last_name_{sx}")()
        first = getattr(fake, f"first_name_{sx}")()
        middle = getattr(fake, f"middle_name_{sx}")() if r.random() < 0.95 else None
        full = f"{last} {first}" + (f" {middle}" if middle else "")
        refs.drivers.append({
            "id": len(refs.drivers) + 1, "last_name": last, "first_name": first,
            "middle_name": middle, "full_name": full, "license": lic,
            "phone": "+380" + "".join(r.choice("0123456789") for _ in range(9)),
        })

    codes = set()
    while len(refs.customers) < N_CUSTOMERS:
        tax = "".join(r.choice("0123456789") for _ in range(8))
        if tax in codes:
            continue
        codes.add(tax)
        city_id = r.randint(1, 60)
        name = f"{r.choice(COMPANY_PREFIXES)} «{fake.company()}»"[:200]
        refs.customers.append({
            "id": len(refs.customers) + 1, "name": name, "tax_code": tax,
            "city_id": city_id, "city": CITIES[city_id - 1][0],
        })
    return refs


def build_refs(seed: int = REF_SEED) -> Refs:
    """Довідники (кешуються за seed, повернені об'єкти не змінювати)."""
    return _build_refs(seed)


# --------------------------------------------------------------------------- тексти
CARGO_TITLES = {
    "general": ["Будівельні матеріали", "Побутова техніка", "Меблі", "Одяг і взуття", "Паперова продукція",
                "Автозапчастини", "Металопрокат", "Упаковка", "Канцелярія", "Електроніка", "Зерно в мішках",
                "Садовий інвентар"],
    "reefer": ["Молочна продукція", "М'ясо охолоджене", "Заморожена риба", "Свіжі овочі", "Фрукти",
               "Морозиво", "Ліки (холодовий ланцюг)", "Квіти зрізані", "Кондитерські вироби"],
    "adr": ["Паливо дизельне", "Лакофарбові матеріали", "Розчинники", "Мінеральні добрива", "Кислота сульфатна",
            "Газові балони", "Побутова хімія", "Клей промисловий"],
    "oversize": ["Трансформатор", "Екскаватор", "Промисловий котел", "Лопать вітротурбіни", "Металева конструкція",
                 "Прес гідравлічний", "Збірний модуль"],
}
CHECKPOINTS = [
    "Пост ДПС на в'їзді", "Кордон області", "Зважувальний пункт", "Мотель «Дорожній»", "Паркінг біля траси",
    "Об'їзна міста", "Міст через Дніпро", "Розвилка М06/Е40", "Автовокзал", "Логістичний хаб",
    "Транспортна розв'язка", "Контрольний пункт", "Перевал", "Склад проміжний", "Митний термінал",
    "Траса М05, 120 км", "Траса М03, 85 км", "Траса М10, 200 км", "Траса М12, 60 км", "Траса Н31, 15 км",
]
FUEL_STATIONS = ["OKKO", "WOG", "SOCAR", "UPG", "БРСМ-Нафта", "Shell", "KLO", "Glusco", "AMIC", "Chipo"]
IDLE_REASONS = ["Затор на дорозі", "Відпочинок водія", "Очікування розвантаження", "Технічний огляд",
                "Погодні умови", "Черга на митниці", "Очікування документів", "Ремонт колеса"]
RECEIVERS = [
    "Коваленко І.П.", "Шевченко О.В.", "Бондаренко М.С.", "Ткаченко А.Ю.", "Кравчук В.О.", "Олійник Н.Д.",
    "Мельник Р.І.", "Лисенко Т.М.", "Мороз Д.А.", "Петренко С.Б.", "Савченко Л.К.", "Гриценко Ю.Є.",
    "Клименко О.Р.", "Руденко В.П.", "Поліщук Б.М.", "Остапенко Г.С.", "Яковенко К.О.", "Сидоренко П.Л.",
]
INCIDENT_DESC = {
    "accident": ["Зіткнення на перехресті", "ДТП із легковим авто", "З'їзд у кювет"],
    "fine": ["Перевищення швидкості", "Перевищення осьового навантаження", "Порушення режиму праці водія"],
    "breakdown": ["Несправність гальм", "Пошкодження турбіни", "Поломка підвіски", "Проблеми зі зчепленням"],
    "damage": ["Пошкодження вантажу під час розвантаження", "Подряпини на причепі", "Пошкодження упаковки"],
}
INCIDENT_TYPES = list(INCIDENT_DESC)
CANCEL_REASONS = ["Відмова замовника", "Відсутність авто", "Хвороба водія", "Зміна маршруту",
                  "Помилка в документах", "Форс-мажор", "Дублювання замовлення"]
ADR_OTHER_CLASSES = [1, 2, 4, 5, 6, 7, 8, 9]
PACKING_GROUPS = ["I", "II", "III"]


# --------------------------------------------------------------------------- ваги
def _weights(k: int, s: float, seed: int, salt: int) -> np.ndarray:
    w = 1.0 / np.arange(1, k + 1) ** s
    w = w[np.random.default_rng([seed, 7, salt]).permutation(k)]  # щоб великі не були завжди з id=1
    return w / w.sum()


def _cargo(r: random.Random) -> list[dict]:
    x = r.random()
    k = 1 if x < 0.15 else 2 if x < 0.35 else 3 if x < 0.65 else 4 if x < 0.85 else 5
    out = []
    for _ in range(k):
        y = r.random()
        if y < 0.60:
            out.append({"kind": "general", "title": r.choice(CARGO_TITLES["general"]),
                        "kg": r.randint(200, 22000), "packages": r.randint(1, 600)})
        elif y < 0.80:
            tmin = r.randint(-25, 2)
            out.append({"kind": "reefer", "title": r.choice(CARGO_TITLES["reefer"]),
                        "kg": r.randint(500, 20000), "temp_min": tmin, "temp_max": tmin + r.randint(2, 10)})
        elif y < 0.90:
            cls = 3 if r.random() < 0.35 else r.choice(ADR_OTHER_CLASSES)
            out.append({"kind": "adr", "title": r.choice(CARGO_TITLES["adr"]),
                        "kg": r.randint(100, 18000), "un_number": f"{r.randint(1000, 3500):04d}",
                        "adr_class": cls, "packing_group": r.choice(PACKING_GROUPS)})
        else:
            out.append({"kind": "oversize", "title": r.choice(CARGO_TITLES["oversize"]),
                        "kg": r.randint(3000, 40000), "length_cm": r.randint(1300, 3000),
                        "width_cm": r.randint(250, 400), "height_cm": r.randint(300, 480)})
    return out


def _dt(m: int) -> datetime:
    return EPOCH + timedelta(minutes=m)


def _events(r: random.Random, status: str, ps: int, km: float, dur_h: float):
    """Повертає (events, started_at, finished_at, odo_dep, odo_last, fuel_c, cost_c)."""
    dep = ps + r.randint(-30, 90)
    total_km = max(1, int(km * r.uniform(1.0, 1.12)))
    dur = max(60, int(dur_h * 60 * r.uniform(0.9, 1.4)))
    dlv = dep + dur
    odo0 = r.randint(20000, 900000)

    mid = ["checkpoint"] * r.randint(2, 6) + ["fuel"] * r.randint(1, 3) + ["idle"] * r.randint(0, 3)
    if r.random() < 0.10:
        mid.append("incident")
    r.shuffle(mid)
    times = sorted(r.randint(dep + 1, dlv - 1) for _ in mid)
    if status == "in_progress":
        keep = r.randint(0, len(mid))
        mid, times = mid[:keep], times[:keep]

    ev = [{"type": "departed", "at": _dt(dep), "odometer": odo0}]
    odo_last = odo0
    liters_c = cost_c = 0
    for t, typ in zip(times, mid):
        at = _dt(t)
        if typ == "idle":
            ev.append({"type": "idle", "at": at, "minutes": r.randint(10, 240),
                       "reason": r.choice(IDLE_REASONS)})
        elif typ == "incident":
            it = r.choice(INCIDENT_TYPES)
            ev.append({"type": "incident", "at": at, "incident_type": it,
                       "description": r.choice(INCIDENT_DESC[it]),
                       "amount": _d2(r.randint(50000, 20000000)), "at_fault": r.random() < 0.4})
        else:
            odo = odo0 + (t - dep) * total_km // dur
            odo_last = odo
            if typ == "checkpoint":
                ev.append({"type": "checkpoint", "at": at, "odometer": odo,
                           "location": r.choice(CHECKPOINTS)})
            else:
                lc, pc = r.randint(15000, 45000), r.randint(5000, 7500)
                liters_c += lc
                cost_c += (lc * pc + 50) // 100
                ev.append({"type": "fuel", "at": at, "odometer": odo, "liters": _d2(lc),
                           "unit_price": _d2(pc), "station": r.choice(FUEL_STATIONS)})
    finished = None
    if status == "completed":
        odo_last = odo0 + total_km
        finished = _dt(dlv)
        ev.append({"type": "delivered", "at": finished, "odometer": odo_last,
                   "receiver": r.choice(RECEIVERS)})
    return ev, ev[0]["at"], finished, odo0, odo_last, liters_c, cost_c


def _gen(n: int, seed: int, salt: int, w0: int, w1: int, late_bias: bool,
         first_id: int, number_fn) -> Iterator[dict]:
    refs = build_refs(REF_SEED)
    rng = np.random.default_rng([seed, salt, 1])
    r = random.Random(f"trips-{seed}-{salt}")

    status_idx = rng.choice(4, size=n, p=STATUS_P)
    starts = rng.integers(w0, w1 + 1, size=n)
    if late_bias:  # planned і in_progress здебільшого в останні місяці періоду
        late = np.flatnonzero(status_idx >= 2)
        near = rng.random(late.size) < 0.8
        starts[late] = np.where(near, rng.integers(w1 - 60 * 1440, w1 + 1, size=late.size),
                                rng.integers(w1 - 180 * 1440, w1 + 1, size=late.size))
    order = np.argsort(starts, kind="stable")
    starts, status_idx = starts[order], status_idx[order]

    pc, pd_, pv = (_weights(N_CUSTOMERS, 1.1, seed, 1), _weights(N_DRIVERS, 0.5, seed, 2),
                   _weights(N_VEHICLES, 0.5, seed, 3))

    veh = [{"id": v["id"], "plate": v["plate"], "vin": v["vin"], "model": v["model"]} for v in refs.vehicles]
    drv = [{"id": d["id"], "full_name": d["full_name"], "license": d["license"]} for d in refs.drivers]
    cus = [{"id": c["id"], "name": c["name"], "tax_code": c["tax_code"], "city": c["city"]}
           for c in refs.customers]
    rts = [{"id": x["id"], "code": x["code"], "from": x["from"], "to": x["to"], "km": x["distance_km"]}
           for x in refs.routes]
    r_km = [float(x["distance_km"]) for x in refs.routes]
    r_h = [float(x["duration_hours"]) for x in refs.routes]

    BLOCK = 50_000
    for b0 in range(0, n, BLOCK):
        m = min(BLOCK, n - b0)
        ci = rng.choice(N_CUSTOMERS, size=m, p=pc).tolist()
        di = rng.choice(N_DRIVERS, size=m, p=pd_).tolist()
        vi = rng.choice(N_VEHICLES, size=m, p=pv).tolist()
        ri = rng.integers(0, N_ROUTES, size=m).tolist()
        for j in range(m):
            k = b0 + j
            status = STATUSES[status_idx[k]]
            ps = int(starts[k])
            rt = ri[j]
            km, dur_h = r_km[rt], r_h[rt]
            planned_start = _dt(ps)
            planned_end = _dt(ps + int(dur_h * 60) + 60 + r.randint(0, 120))
            cancel_reason = None
            started = finished = None
            ev: list = []
            distance = None
            fuel_l = fuel_cost = 0
            if status in ("completed", "in_progress"):
                ev, started, finished, odo0, odo_last, fuel_l, fuel_cost = _events(r, status, ps, km, dur_h)
                distance = odo_last - odo0
            elif status == "cancelled":
                cancel_reason = r.choice(CANCEL_REASONS)
            yield {
                "trip_number": number_fn(k, planned_start.year),
                "trip_id": first_id + k,
                "status": status,
                "vehicle": dict(veh[vi[j]]),
                "driver": dict(drv[di[j]]),
                "customer": dict(cus[ci[j]]),
                "route": dict(rts[rt]),
                "planned_start": planned_start, "planned_end": planned_end,
                "started_at": started, "finished_at": finished,
                "cancel_reason": cancel_reason,
                "cargo": _cargo(r),
                "events": ev,
                "totals": {"distance_km": distance, "fuel_l": _d2(fuel_l), "fuel_cost": _d2(fuel_cost),
                           "freight_price": _d2(int(km * r.uniform(2500, 5500)))},
            }


def generate_trips(n: int, seed: int = 42) -> Iterator[dict]:
    """n канонічних документів, впорядкованих за planned_start; trip_id = 1..n."""
    counters: dict[int, int] = {}

    def number(_k: int, year: int) -> str:
        c = counters.get(year, 0) + 1
        counters[year] = c
        return f"TR-{year}-{c:06d}"

    return _gen(n, seed, 0, P0, P1, True, 1, number)


def extra_trips(count: int, start_trip_id: int, seed: int) -> list[dict]:
    """Нові рейси для S4 і тестів: planned_start від 2026-11-01 до 2026-12-31."""
    assert count <= 99999
    return list(_gen(count, seed, start_trip_id + 1, EX0, EX1, False, start_trip_id,
                     lambda k, _y: f"TR-2026-{900000 + k:06d}"))
