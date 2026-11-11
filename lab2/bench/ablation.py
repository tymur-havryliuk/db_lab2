"""Абляції: додаткові заміри до основного бенчмарку.

s2: додаткове поле в складеному індексі S2 (у Mongo немає Incremental Sort)
snapshot: нормалізований SQL з копіями назв у таблиці рейсів
s3: S3 у SQL окремими запитами проти однієї PL/pgSQL-функції
s4: S4 у PostgreSQL, executemany проти COPY на батчах 100 і 1000
decode: S1 без перетворення в канонічний dict
growth: 1000 дописувань до одного рейсу, латентність S3 і S1
s8mix: S8 без важкого S7 (суміш 65/20/15/0 замість 60/20/15/5)
audit: S3 і S4 у SQL без тригерів аудиту і з ними, поряд Mongo
hybrid: створення рейсу в SQL, у Mongo, PG потім Mongo послідовно (sync) і через outbox

Кожна абляція прибирає за собою тимчасові індекси, таблиці, рейси і назви.
Запуск: uv run python -m lab2.bench.ablation --scale 1m [--only s2,snapshot,...]
audit і hybrid не входять у повний прогін і дописують рядки в ablation.csv.
"""
from __future__ import annotations

import argparse
import gc
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bson.raw_bson import RawBSONDocument

from lab2 import codec
from lab2 import equivalence as eq
from lab2.bench import concurrency
from lab2.bench.common import RESULTS, CsvSink, summarize
from lab2.bench.runner import timed
from lab2.db import mongo_trips, pg_connect
from lab2.repos import open_repo
from lab2.repos import sql as sqlmod

UTC = timezone.utc
FIELDS = ["scale", "ablation", "scenario", "variant", "mode", "n", "p50_ms", "p95_ms", "p99_ms", "extra"]


class Ablations:
    def __init__(self, scale: str, seed: int):
        self.scale, self.seed = scale, seed
        self.out = RESULTS / scale
        self.repos = {v: open_repo(v) for v in ("sql", "jsonb", "mongo")}
        self.uni = eq.load_universe(self.repos["sql"])
        self.sink = CsvSink(self.out / "ablation.csv", FIELDS)

    def rng(self, sc: str, stream: str) -> random.Random:
        return random.Random(f"{self.seed}:{sc}:{stream}")

    def measure(self, ablation, scenario, variant, mode, fn_list, extra="") -> dict:
        gc.collect(); gc.disable()
        try:
            lat = [timed(f)[0] for f in fn_list]
        finally:
            gc.enable()
        return self.record(ablation, scenario, variant, mode, lat, extra)

    def record(self, ablation, scenario, variant, mode, lat, extra="") -> dict:
        s = summarize(lat)
        self.sink.writemany([{"scale": self.scale, "ablation": ablation, "scenario": scenario, "variant": variant,
                              "mode": mode, "n": s["n"], "p50_ms": round(s["p50_ms"], 4),
                              "p95_ms": round(s["p95_ms"], 4), "p99_ms": round(s["p99_ms"], 4), "extra": extra}])
        print(f"  {ablation:8} {scenario:12} {variant:6} {mode:22} p50={s['p50_ms']:9.3f}  p95={s['p95_ms']:9.3f} {extra}",
              flush=True)
        return s

    @staticmethod
    def settle() -> None:
        """Між режимами: VACUUM, CHECKPOINT і fsync (як Bench.settle)."""
        with pg_connect(autocommit=True) as c:
            c.execute("VACUUM ANALYZE bench.trips, bench.trip_events, bench.fuel_transactions, "
                      "bench.trip_cargo_items, bench.incidents")
            c.execute("CHECKPOINT")
        mongo_trips().database.client.admin.command("fsync")
        time.sleep(2)

    # ---------------------------------------------------------------- s2
    def s2(self) -> None:
        extra_idx = {
            "sql": ("CREATE INDEX ab_trips_driver_start_tn ON bench.trips (driver_id, planned_start DESC, trip_number DESC)",
                    "DROP INDEX IF EXISTS bench.ab_trips_driver_start_tn"),
            "jsonb": ("CREATE INDEX ab_docs_driver_start_tn ON bench.trip_docs "
                      "(((doc->'driver'->>'id')::int), (doc->>'planned_start') DESC, trip_number DESC)",
                      "DROP INDEX IF EXISTS bench.ab_docs_driver_start_tn"),
        }
        rw, rm = self.rng("S2", "warmup"), self.rng("S2", "measure")
        warm = [eq.p_s2(rw, self.uni) for _ in range(30)]
        meas = [eq.p_s2(rm, self.uni) for _ in range(200)]      # ті самі параметри, що в основному прогоні
        try:
            for label in ("base_index", "index_with_tiebreak"):
                if label == "index_with_tiebreak":
                    with pg_connect(autocommit=True) as c:
                        for ddl, _ in extra_idx.values():
                            c.execute(ddl)
                        c.execute("ANALYZE bench.trips"); c.execute("ANALYZE bench.trip_docs")
                    mongo_trips().create_index([("driver.id", 1), ("planned_start", -1), ("_id", -1)],
                                               name="ab_driver_start_id")
                    for v in self.repos:              # нові підключення, щоб не було кешованих планів
                        self.repos[v].close(); self.repos[v] = open_repo(v)
                for v, r in self.repos.items():
                    for p in warm:
                        r.driver_trips(**p)
                    ex = r.explain("S2", **meas[0]).notes
                    self.measure("s2", "S2", v, label, [lambda p=p: r.driver_trips(**p) for p in meas],
                                 extra=f"docs_examined={ex.get('totalDocsExamined', '-')} idx={ex.get('index') or ex.get('indexes')}")
        finally:
            with pg_connect(autocommit=True) as c:
                for _, drop in extra_idx.values():
                    c.execute(drop)
            try:
                mongo_trips().drop_index("ab_driver_start_id")
            except Exception:
                pass
            for v in self.repos:
                self.repos[v].close(); self.repos[v] = open_repo(v)

    # ---------------------------------------------------------------- snapshot
    SNAP_DDL = """
    DROP TABLE IF EXISTS bench.ab_trips_snap;
    CREATE TABLE bench.ab_trips_snap AS
    SELECT t.*, v.plate_number AS s_plate, v.vin AS s_vin, v.model AS s_model,
           concat_ws(' ', d.last_name, d.first_name, d.middle_name) AS s_driver, d.license_number AS s_license,
           c.name AS s_customer, c.tax_code AS s_tax, cc.name AS s_city,
           r.code AS s_route_code, cf.name AS s_from, ct.name AS s_to, r.distance_km AS s_km
      FROM bench.trips t
      JOIN bench.vehicles v ON v.vehicle_id = t.vehicle_id
      JOIN bench.drivers d ON d.driver_id = t.driver_id
      JOIN bench.customers c ON c.customer_id = t.customer_id
      LEFT JOIN bench.cities cc ON cc.city_id = c.city_id
      JOIN bench.routes r ON r.route_id = t.route_id
      JOIN bench.cities cf ON cf.city_id = r.from_city_id
      JOIN bench.cities ct ON ct.city_id = r.to_city_id;
    ALTER TABLE bench.ab_trips_snap ADD PRIMARY KEY (trip_id);
    CREATE UNIQUE INDEX ab_snap_number ON bench.ab_trips_snap (trip_number);
    CREATE INDEX ab_snap_customer ON bench.ab_trips_snap (customer_id);
    VACUUM ANALYZE bench.ab_trips_snap;
    """

    @staticmethod
    def snap_s1_sql() -> str:
        q = sqlmod.S1_SQL
        for a, b in (("v.vehicle_id, v.plate_number, v.vin, v.model,", "t.vehicle_id, t.s_plate, t.s_vin, t.s_model,"),
                     ("d.driver_id, concat_ws(' ', d.last_name, d.first_name, d.middle_name), d.license_number,",
                      "t.driver_id, t.s_driver, t.s_license,"),
                     ("c.customer_id, c.name, c.tax_code, cc.name,", "t.customer_id, t.s_customer, t.s_tax, t.s_city,"),
                     ("r.route_id, r.code, cf.name, ct.name, r.distance_km,",
                      "t.route_id, t.s_route_code, t.s_from, t.s_to, t.s_km,")):
            assert a in q, a
            q = q.replace(a, b)
        head, tail = q.split("  FROM bench.trips t", 1)
        return head + "  FROM bench.ab_trips_snap t\n WHERE t.trip_number = %s\n"

    def snapshot(self) -> None:
        sql = self.repos["sql"]
        with pg_connect(autocommit=True) as c:
            for stmt in filter(str.strip, self.SNAP_DDL.split(";")):   # VACUUM не можна в запиті з кількома операторами
                c.execute(stmt)
            c.execute("CHECKPOINT")
        snap_sql = self.snap_s1_sql()

        def get_snap(tn):
            old, sqlmod.S1_SQL = sqlmod.S1_SQL, snap_sql
            try:
                return sql.get_trip(tn)
            finally:
                sqlmod.S1_SQL = old
        try:
            # знімок щойно скопійований, тому S1 має збігатися з нормалізованим
            r = self.rng("S1", "measure")
            meas = [eq.p_s1(r, self.uni)["trip_number"] for _ in range(200)]
            bad = [tn for tn in meas[:50] if eq.diff(eq.normalize(get_snap(tn)), eq.normalize(sql.get_trip(tn)))]
            assert not bad, f"знімок не збігається: {bad[:3]}"
            warm = [eq.p_s1(self.rng("S1", "warmup"), self.uni)["trip_number"] for _ in range(30)]
            for tn in warm:
                get_snap(tn); sql.get_trip(tn)
            self.measure("snapshot", "S1", "sql", "normalized_joins", [lambda tn=tn: sql.get_trip(tn) for tn in meas])
            self.measure("snapshot", "S1", "sql", "snapshot_columns", [lambda tn=tn: get_snap(tn) for tn in meas])

            # S6 зі знімком: назва в довіднику і копії в рейсах (усі або лише відкриті)
            with sql.conn.cursor() as cur:
                names = dict(cur.execute("SELECT customer_id, name FROM bench.customers").fetchall())
                top = cur.execute("SELECT customer_id FROM bench.trips GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
            cust = self.rng("S6", "measure").sample(self.uni["customers"], 15)

            def rename(cid, name, scope):
                st = "" if scope == "all" else " AND status IN ('planned', 'in_progress')"
                with sql.conn.transaction():
                    sql.conn.execute("UPDATE bench.customers SET name = %s WHERE customer_id = %s", (name, cid))
                    return sql.conn.execute(f"UPDATE bench.ab_trips_snap SET s_customer = %s WHERE customer_id = %s{st}",
                                            (name, cid)).rowcount
            try:
                for scope in ("all", "open"):
                    changed = []
                    fns = []
                    for cid in cust:
                        fns.append(lambda cid=cid: changed.append(rename(cid, names[cid] + " [snap]", scope)))
                    s = self.measure("snapshot", f"S6{scope}", "sql", "snapshot_columns", fns)
                    us, n_top = timed(lambda: rename(top, names[top] + " [snap]", scope))
                    self.sink.writemany([{"scale": self.scale, "ablation": "snapshot", "scenario": f"S6{scope}_top",
                                          "variant": "sql", "mode": "snapshot_columns", "n": 1,
                                          "p50_ms": round(us / 1000, 3), "extra": f"changed={n_top}"}])
                    print(f"  snapshot S6{scope} top customer: {us/1000:.1f} ms, changed={n_top}; "
                          f"median changed={sorted(changed)[len(changed)//2]}", flush=True)
            finally:
                for cid in set(cust + [top]):
                    sql.conn.execute("UPDATE bench.customers SET name = %s WHERE customer_id = %s", (names[cid], cid))
        finally:
            with pg_connect(autocommit=True) as c:
                c.execute("DROP TABLE IF EXISTS bench.ab_trips_snap")

    # ---------------------------------------------------------------- s3
    def s3(self) -> None:
        sql, open_trips = self.repos["sql"], self.uni["by_status"]["in_progress"]
        base = datetime(2026, 11, 20, tzinfo=UTC)
        for kind in ("checkpoint", "fuel"):
            for mode, fn in (("multi_statement", sql.append_event_multi), ("plpgsql_function", sql.append_event)):
                r = self.rng(f"S3-{kind}", mode)
                trips = [r.choice(open_trips) for _ in range(220)]
                evs = []
                for i in range(220):
                    e = {"type": kind, "at": base + timedelta(minutes=i), "odometer": 700_000 + i}
                    e.update({"location": "Абляція"} if kind == "checkpoint" else
                             {"liters": Decimal("100.00"), "unit_price": Decimal("50.00"), "station": "Абляція"})
                    evs.append(e)
                for tn, e in zip(trips[:20], evs[:20]):
                    fn(tn, e)
                # події дописуються в усі три варіанти однаково
                for v in ("jsonb", "mongo"):
                    for tn, e in zip(trips, evs):
                        self.repos[v].append_event(tn, e)
                self.measure("s3", f"S3-{kind}", "sql", mode,
                             [lambda tn=tn, e=e: fn(tn, e) for tn, e in zip(trips[20:], evs[20:])])

    # ---------------------------------------------------------------- s4
    def s4(self) -> None:
        from lab2.generator import extra_trips
        for v in ("sql", "jsonb"):
            repo = self.repos[v]
            for mode in ("executemany", "copy"):
                fn = repo.insert_trips_executemany if mode == "executemany" else repo.insert_trips_copy
                for bsize, nb in ((100, 20), (1000, 5)):
                    docs = extra_trips(bsize * nb, self.uni["max_trip_id"] + 1, seed=self.seed + bsize)
                    batches = [docs[i:i + bsize] for i in range(0, len(docs), bsize)]
                    try:
                        s = self.measure("s4", f"S4_b{bsize}", v, mode, [lambda b=b: fn(b) for b in batches])
                        self.sink.writemany([{"scale": self.scale, "ablation": "s4", "scenario": f"S4_b{bsize}_tps",
                                              "variant": v, "mode": mode, "n": nb,
                                              "p50_ms": round(bsize / (s["p50_ms"] / 1000), 1), "extra": "trips/s за p50"}])
                    finally:
                        repo.delete_trips([d["trip_number"] for d in docs])

    # ---------------------------------------------------------------- decode
    def decode(self) -> None:
        r = self.rng("S1", "measure")
        meas = [eq.p_s1(r, self.uni)["trip_number"] for _ in range(200)]
        sql, jsonb = self.repos["sql"], self.repos["jsonb"]
        raw_coll = mongo_trips().with_options(codec_options=mongo_trips().codec_options.with_options(
            document_class=RawBSONDocument, type_registry=None))
        variants = {
            "sql": (lambda tn: sql.get_trip(tn), lambda tn: sql.conn.execute(sqlmod.S1_SQL, (tn,)).fetchone()),
            "jsonb": (lambda tn: jsonb.get_trip(tn),
                      lambda tn: jsonb.conn.execute("SELECT doc::text FROM bench.trip_docs WHERE trip_number = %s",
                                                    (tn,)).fetchone()),
            "mongo": (lambda tn: self.repos["mongo"].get_trip(tn), lambda tn: raw_coll.find_one({"_id": tn})),
        }
        for v, (full, raw) in variants.items():
            for tn in meas[:30]:
                full(tn); raw(tn)
            self.measure("decode", "S1", v, "full_canonical_dict", [lambda tn=tn: full(tn) for tn in meas])
            self.measure("decode", "S1", v, "raw_fetch_no_decode", [lambda tn=tn: raw(tn) for tn in meas],
                         extra="sql: рядок з json_agg без збирання dict; jsonb: текст; mongo: RawBSONDocument")

    # ---------------------------------------------------------------- growth
    def growth(self) -> None:
        from lab2.generator import extra_trips
        doc = extra_trips(1, self.uni["max_trip_id"] + 1, seed=self.seed + 77)[0]
        tn, base = doc["trip_number"], doc["planned_start"] + timedelta(hours=1)
        sink = CsvSink(self.out / "ablation_growth.csv", ["scale", "variant", "events_before", "append_us", "s1_us"])
        try:
            for v, repo in self.repos.items():
                repo.insert_trips([doc])
                n0 = len(doc["events"])
                for i in range(1000):
                    ev = {"type": "checkpoint", "at": base + timedelta(minutes=i), "odometer": 800_000 + i,
                          "location": f"Пункт {i}"}
                    us, _ = timed(lambda: repo.append_event(tn, ev))
                    s1 = timed(lambda: repo.get_trip(tn))[0] if i % 50 == 0 or i == 999 else ""
                    sink.write({"scale": self.scale, "variant": v, "events_before": n0 + i,
                                "append_us": round(us, 1), "s1_us": round(s1, 1) if s1 != "" else ""})
                print(f"  growth   {v}: готово", flush=True)
        finally:
            for repo in self.repos.values():
                repo.delete_trips([tn])
            sink.close()

    # ---------------------------------------------------------------- s8mix
    def s8mix(self) -> None:
        from lab2.generator import extra_trips
        mix = (("S1", 0.65), ("S2", 0.20), ("S3a", 0.15))
        tmp = extra_trips(2000, self.uni["max_trip_id"] + 1, seed=self.seed + 8)
        uni = {**self.uni, "by_status": {**self.uni["by_status"], "in_progress": [d["trip_number"] for d in tmp]}}
        (self.out / "ablation_s8_noS7.csv").unlink(missing_ok=True)
        for v in self.repos:
            self.repos[v].insert_trips(tmp)
            try:
                for workers in (1, 8, 32):
                    res = concurrency.run(v, workers, 20, uni, self.seed, mix=mix)
                    concurrency.save(self.out, self.scale, v, workers, 20, res, fname="ablation_s8_noS7.csv")
            finally:
                self.repos[v].delete_trips([d["trip_number"] for d in tmp])


    # ---------------------------------------------------------------- audit
    # Те саме, що change_log у лабі 1: журнал і тригер на кожен INSERT, UPDATE і DELETE (to_jsonb(OLD/NEW)).
    # Відмінності: немає app_users (changed_by без зовнішнього ключа), а record_id це перший стовпець
    # ідентифікатора таблиці (trip_id, fuel_tx_id або incident_id).
    AUDIT_DDL = """
    CREATE TYPE bench.change_action AS ENUM ('created', 'updated', 'deleted', 'archived', 'restored');
    CREATE TABLE bench.change_log (
        change_id   BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        table_name  VARCHAR(63)   NOT NULL,
        record_id   INT           NOT NULL,
        action      bench.change_action NOT NULL,
        changed_by  INT,
        changed_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
    );
    CREATE TABLE bench.change_log_fields (
        change_id   BIGINT       NOT NULL REFERENCES bench.change_log ON DELETE CASCADE,
        field_name  VARCHAR(63)  NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        PRIMARY KEY (change_id, field_name)
    );
    CREATE FUNCTION bench.acting_user_id() RETURNS INT LANGUAGE sql STABLE AS $$
        SELECT NULLIF(current_setting('fleet.user_id', true), '')::INT;
    $$;
    CREATE FUNCTION bench.tg_write_change_log() RETURNS TRIGGER LANGUAGE plpgsql AS $$
    DECLARE
        c_ignored   CONSTANT TEXT[] := ARRAY['modified_at', 'modified_by'];
        c_id_column CONSTANT TEXT   := TG_ARGV[0];
        v_old       JSONB := CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END;
        v_new       JSONB := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
        v_action    bench.change_action;
        v_change_id BIGINT;
    BEGIN
        IF TG_OP = 'UPDATE' AND NOT EXISTS (
            SELECT 1 FROM jsonb_each(v_new) AS field
            WHERE field.key <> ALL (c_ignored) AND field.value IS DISTINCT FROM v_old -> field.key
        ) THEN
            RETURN NULL;
        END IF;

        v_action := CASE
            WHEN TG_OP = 'INSERT' THEN 'created'
            WHEN TG_OP = 'DELETE' THEN 'deleted'
            WHEN v_old ->> 'deleted_at' IS NULL AND v_new ->> 'deleted_at' IS NOT NULL THEN 'archived'
            WHEN v_old ->> 'deleted_at' IS NOT NULL AND v_new ->> 'deleted_at' IS NULL THEN 'restored'
            ELSE 'updated'
        END;

        INSERT INTO bench.change_log (table_name, record_id, action, changed_by)
        VALUES (TG_TABLE_NAME, (COALESCE(v_new, v_old) ->> c_id_column)::INT, v_action, bench.acting_user_id())
        RETURNING change_id INTO v_change_id;

        IF TG_OP = 'UPDATE' THEN
            INSERT INTO bench.change_log_fields (change_id, field_name, old_value, new_value)
            SELECT v_change_id, field.key, v_old ->> field.key, v_new ->> field.key
            FROM jsonb_each(v_new) AS field
            WHERE field.key <> ALL (c_ignored) AND field.value IS DISTINCT FROM v_old -> field.key;
        END IF;
        RETURN NULL;
    END $$;
    """
    AUDIT_TABLES = (("trips", "trip_id"), ("trip_cargo_items", "trip_id"), ("trip_events", "trip_id"),
                    ("fuel_transactions", "fuel_tx_id"), ("incidents", "incident_id"))

    def _audit_on(self) -> None:
        with pg_connect(autocommit=True) as c:
            c.execute(self.AUDIT_DDL)
            for tb, idc in self.AUDIT_TABLES:
                c.execute(f"CREATE TRIGGER trg_{tb}_change_log AFTER INSERT OR UPDATE OR DELETE ON bench.{tb} "
                          f"FOR EACH ROW EXECUTE FUNCTION bench.tg_write_change_log('{idc}')")

    @staticmethod
    def _audit_off() -> None:
        with pg_connect(autocommit=True) as c:
            for tb, _ in Ablations.AUDIT_TABLES:
                c.execute(f"DROP TRIGGER IF EXISTS trg_{tb}_change_log ON bench.{tb}")
            c.execute("DROP TABLE IF EXISTS bench.change_log_fields, bench.change_log")
            c.execute("DROP FUNCTION IF EXISTS bench.tg_write_change_log()")
            c.execute("DROP FUNCTION IF EXISTS bench.acting_user_id()")
            c.execute("DROP TYPE IF EXISTS bench.change_action")

    def audit(self) -> None:
        from lab2.generator import extra_trips
        sql, mongo = self.repos["sql"], self.repos["mongo"]
        base = datetime(2026, 11, 20, tzinfo=UTC)
        before = {v: self.repos[v].count() for v in ("sql", "mongo")}
        # S3 пише в тимчасові рейси, щоб не змінювати наявні дані; вони є і в SQL, і в Mongo
        plan = [(1, 200), (10, 100), (100, 30), (1000, 10)]                # як S4 в основному прогоні
        allx = extra_trips(500 + sum(b * n for b, n in plan), self.uni["max_trip_id"] + 1, seed=self.seed + 31)
        tmp, docs = allx[:500], allx[500:]                                 # номери рейсів не перетинаються
        tmp_numbers = [d["trip_number"] for d in tmp]
        s4_numbers = [d["trip_number"] for d in docs]
        batches = {}
        pos = 0
        for bsize, nb in plan:
            batches[bsize] = [docs[pos + i * bsize: pos + (i + 1) * bsize] for i in range(nb)]
            pos += bsize * nb
        s3_params = {}
        for kind in ("checkpoint", "fuel"):
            r = self.rng(f"S3-{kind}", "audit")
            evs = []
            for i in range(220):
                e = {"type": kind, "at": base + timedelta(minutes=i), "odometer": 700_000 + i}
                e.update({"location": "Аудит"} if kind == "checkpoint" else
                         {"liters": Decimal("100.00"), "unit_price": Decimal("50.00"), "station": "Аудит"})
                evs.append(e)
            s3_params[kind] = list(zip([r.choice(tmp_numbers) for _ in range(220)], evs))

        def s3_phase(label, variant, repo):
            for kind, ps in s3_params.items():
                for tn, e in ps[:20]:
                    repo.append_event(tn, e)
                self.measure("audit", f"S3-{kind}", variant, label,
                             [lambda tn=tn, e=e: repo.append_event(tn, e) for tn, e in ps[20:]])

        def s4_phase(label, variant, repo, cleanup=True):
            for bsize, bl in batches.items():
                s = self.measure("audit", f"S4_b{bsize}", variant, label, [lambda b=b: repo.insert_trips(b) for b in bl])
                self.sink.writemany([{"scale": self.scale, "ablation": "audit", "scenario": f"S4_b{bsize}_tps",
                                      "variant": variant, "mode": label, "n": len(bl),
                                      "p50_ms": round(bsize / (s["p50_ms"] / 1000), 1), "extra": "trips/s за p50"}])
            if cleanup:                    # з аудитом рейси видаляє finally, коли тригерів уже немає
                repo.delete_trips(s4_numbers)

        try:
            sql.insert_trips(tmp); mongo.insert_trips(tmp)
            for label in ("no_audit", "with_audit"):
                if label == "with_audit":
                    self._audit_on()
                    for v in ("sql", "mongo"):
                        self.repos[v].close(); self.repos[v] = open_repo(v)
                    sql, mongo = self.repos["sql"], self.repos["mongo"]
                    with pg_connect() as c:
                        c.execute("ANALYZE bench.change_log")
                self.settle()
                s3_phase(label, "sql", sql)
                if label == "with_audit":                                  # тригери спрацьовують
                    with pg_connect() as c:
                        n3 = c.execute("SELECT count(*) FROM bench.change_log").fetchone()[0]
                    assert n3 == 220 + 2 * 220, f"S3: очікувалось 660 рядків журналу, є {n3}"
                    print(f"  audit    після S3 у change_log {n3} рядків (220 checkpoint + 220 fuel x 2 таблиці)", flush=True)
                if label == "no_audit":                                    # Mongo без аудиту, один набір
                    s3_phase("no_audit", "mongo", mongo)
                self.settle()
                s4_phase(label, "sql", sql, cleanup=label == "no_audit")
                if label == "no_audit":
                    self.settle()
                    s4_phase("no_audit", "mongo", mongo)
                if label == "with_audit":
                    with pg_connect() as c:
                        n = c.execute("SELECT count(*) FROM bench.change_log").fetchone()[0]
                    print(f"  audit    рядків у change_log наприкінці: {n}", flush=True)
                    self.sink.writemany([{"scale": self.scale, "ablation": "audit", "scenario": "change_log_rows",
                                          "variant": "sql", "mode": label, "n": n, "extra": "рядків журналу"}])
        finally:
            self._audit_off()               # спершу прибираємо тригери, потім видаляємо рейси, щоб не писати в журнал
            for v in ("sql", "mongo"):
                self.repos[v].close(); self.repos[v] = open_repo(v)
            for repo in (self.repos["sql"], self.repos["mongo"]):
                repo.delete_trips(tmp_numbers + s4_numbers)
            after = {v: self.repos[v].count() for v in ("sql", "mongo")}
            assert after == before, f"audit: дані не повернулись: {before} != {after}"
            print("  audit    дані повернуто до початкового стану", flush=True)

    # ---------------------------------------------------------------- hybrid
    OUTBOX_DDL = """
    DROP TABLE IF EXISTS bench.outbox;
    CREATE TABLE bench.outbox (
        trip_number text PRIMARY KEY,
        payload     jsonb NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX ix_outbox_created ON bench.outbox (created_at);
    """

    @staticmethod
    def _insert_with_outbox(conn, doc: dict) -> None:
        """Рейс у нормалізовані таблиці і рядок в outbox, в одній транзакції."""
        with conn.transaction(), conn.cursor() as cur:
            for table, rows in codec.to_sql_rows(doc).items():
                if rows:
                    cols = codec.SQL_COLUMNS[table]
                    cur.executemany(f"INSERT INTO bench.{table} ({', '.join(cols)}) "
                                    f"VALUES ({', '.join(['%s'] * len(cols))})", rows)
            cur.execute("INSERT INTO bench.outbox (trip_number, payload) VALUES (%s, %s::jsonb)",
                        (doc["trip_number"], codec.to_jsonb(doc)))

    @staticmethod
    def _ship(conn, mongo, batch: int = 100) -> int:
        """Один цикл доставника: пачка з outbox (SKIP LOCKED), insert_many(ordered=False), видалення з outbox.
        Повертає кількість оброблених рядків (0, якщо outbox порожній). Дублікати за _id ігноруються."""
        from pymongo.errors import BulkWriteError
        with conn.transaction():
            rows = conn.execute("SELECT trip_number, payload::text FROM bench.outbox ORDER BY created_at "
                                "LIMIT %s FOR UPDATE SKIP LOCKED", (batch,)).fetchall()
            if not rows:
                return 0
            docs = [codec.to_mongo(codec.from_jsonb(p)) for _, p in rows]
            try:
                mongo.col.insert_many(docs, ordered=False)
            except BulkWriteError as e:
                if any(err["code"] != 11000 for err in e.details["writeErrors"]):
                    raise
            conn.execute("DELETE FROM bench.outbox WHERE trip_number = ANY(%s)", ([t for t, _ in rows],))
        return len(rows)

    def hybrid(self) -> None:
        from lab2.generator import extra_trips
        sql, mongo = self.repos["sql"], self.repos["mongo"]
        n_warm, n_meas, n_bulk = 30, 200, 2000
        n = n_warm + n_meas
        total = 4 * n + n_bulk
        docs = extra_trips(total, self.uni["max_trip_id"] + 1, seed=self.seed + 41)
        pool = {"sql_only": docs[:n], "mongo_only": docs[n:2 * n], "hybrid_sync": docs[2 * n:3 * n],
                "hybrid_outbox": docs[3 * n:4 * n]}
        bulk = docs[4 * n:]
        all_numbers = [d["trip_number"] for d in docs]
        before = {v: self.repos[v].count() for v in ("sql", "mongo")}
        with pg_connect(autocommit=True) as c:
            c.execute(self.OUTBOX_DDL)
        conn = sql.conn
        try:
            def run(mode, fn, docs_):
                for d in docs_[:n_warm]:
                    fn(d)
                gc.collect(); gc.disable()
                try:
                    return [timed(lambda d=d: fn(d)) for d in docs_[n_warm:]]
                finally:
                    gc.enable()

            for mode in ("sql_only", "mongo_only"):
                self.settle()
                res = run(mode, (lambda d: sql.insert_trips([d])) if mode == "sql_only"
                          else (lambda d: mongo.insert_trips([d])), pool[mode])
                self.record("hybrid", "create_trip", mode.split("_")[0], mode, [t for t, _ in res])

            # hybrid_sync: спочатку commit у PG, потім Mongo; обидві частини міряємо окремо
            self.settle()
            parts = []

            def sync(d):
                t1, _ = timed(lambda: sql.insert_trips([d]))
                t2, _ = timed(lambda: mongo.insert_trips([d]))
                parts.append((t1, t2))
            res = run("hybrid_sync", sync, pool["hybrid_sync"])
            parts = parts[n_warm:]
            tot = [t for t, _ in res]
            ps = summarize(p[0] for p in parts)["p50_ms"]
            ms = summarize(p[1] for p in parts)["p50_ms"]
            self.record("hybrid", "create_trip", "pg+mongo", "hybrid_sync", tot, extra=f"p50 частин: pg={ps:.3f} mongo={ms:.3f} мс")
            self.record("hybrid", "create_trip", "sql", "hybrid_sync_pg_part", [p[0] for p in parts])
            self.record("hybrid", "create_trip", "mongo", "hybrid_sync_mongo_part", [p[1] for p in parts])

            # hybrid_outbox: користувач чекає тільки транзакцію PG (рейс і outbox)
            self.settle()
            res = run("hybrid_outbox", lambda d: self._insert_with_outbox(conn, d), pool["hybrid_outbox"])
            self.record("hybrid", "create_trip", "pg+outbox", "hybrid_outbox_commit", [t for t, _ in res])

            # доставник: у черзі лишились n рейсів з виміру і n_bulk рядків, доданих напряму в outbox
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany("INSERT INTO bench.outbox (trip_number, payload) VALUES (%s, %s::jsonb)",
                                [(d["trip_number"], codec.to_jsonb(d)) for d in bulk])
            queued = conn.execute("SELECT count(*) FROM bench.outbox").fetchone()[0]
            self.settle()
            lat, done = [], 0
            t0 = time.perf_counter()
            while True:
                us, k = timed(lambda: self._ship(conn, mongo, 100))
                if k == 0:
                    break
                lat.append(us); done += k
            wall = time.perf_counter() - t0
            assert done == queued == n + n_bulk, (done, queued)
            self.record("hybrid", "outbox_shipper", "pg→mongo", "batch100", lat,
                        extra=f"{done / wall:.0f} trips/s ({done} рейсів за {wall:.2f} с; p50 — латентність пачки 100)")
            self.sink.writemany([{"scale": self.scale, "ablation": "hybrid", "scenario": "outbox_shipper_tps",
                                  "variant": "pg→mongo", "mode": "batch100", "n": done,
                                  "p50_ms": round(done / wall, 1), "extra": "trips/s (повний дренаж)"}])

            # однакові рейси в обох базах
            ids = {k: [d["trip_number"] for d in v[:]] for k, v in pool.items()}
            q = lambda nums: set(r[0] for r in conn.execute(
                "SELECT trip_number FROM bench.trips WHERE trip_number = ANY(%s)", (nums,)))
            m = lambda nums: set(mongo.col.distinct("_id", {"_id": {"$in": nums}}))
            for k in ("hybrid_sync", "hybrid_outbox"):
                assert q(ids[k]) == m(ids[k]) == set(ids[k]), f"{k}: набори рейсів у PG і Mongo різні"
            assert len(q(ids["sql_only"])) == n and not m(ids["sql_only"]), "sql_only"
            assert len(m(ids["mongo_only"])) == n and not q(ids["mongo_only"]), "mongo_only"
            assert len(m([d["trip_number"] for d in bulk])) == n_bulk, "bulk у Mongo"
            assert conn.execute("SELECT count(*) FROM bench.outbox").fetchone()[0] == 0
            for d in pool["hybrid_sync"][-3:] + pool["hybrid_outbox"][-3:]:     # документи ідентичні і після outbox
                bad = eq.diff(eq.normalize(sql.get_trip(d["trip_number"])), eq.normalize(mongo.get_trip(d["trip_number"])))
                assert not bad, f"документи PG і Mongo різні: {d['trip_number']}: {bad[:2]}"
            print(f"  hybrid   перевірка: sync={n}, outbox={n} рейсів збігаються в PG і Mongo; "
                  f"доставлено {done}, outbox порожній", flush=True)
        finally:
            with pg_connect(autocommit=True) as c:
                c.execute("DROP TABLE IF EXISTS bench.outbox")
            for repo in (sql, mongo):
                repo.delete_trips(all_numbers)
            after = {v: self.repos[v].count() for v in ("sql", "mongo")}
            assert after == before, f"hybrid: дані не повернулись: {before} != {after}"
            print("  hybrid   дані повернуто до початкового стану", flush=True)

    def close(self) -> None:
        self.sink.close()
        for r in self.repos.values():
            r.close()


ALL = ("s2", "snapshot", "s3", "s4", "decode", "growth", "s8mix", "audit", "hybrid")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", required=True)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--only", default=",".join(ALL))
    a = ap.parse_args()
    only = a.only.split(",")
    out = RESULTS / a.scale
    if set(only) == set(ALL) - {"audit", "hybrid"} or set(only) == set(ALL):
        (out / "ablation.csv").unlink(missing_ok=True)   # CsvSink дописує, тому повний прогін починаємо з нуля
    ab = Ablations(a.scale, a.seed)
    try:
        for name in only:
            print(f"[{name}]", flush=True)
            getattr(ab, name)()
        rep = eq.check_reads(ab.repos, ab.uni, seed=a.seed, n=10)
        print(f"[verify after ablations] розбіжностей: {len(rep.mismatches)}", flush=True)
    finally:
        ab.close()


if __name__ == "__main__":
    main()
