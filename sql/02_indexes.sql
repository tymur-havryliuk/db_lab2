-- Вторинні індекси, які будуються після завантаження даних. PK і UNIQUE лишаються в 01_schema.sql.
-- Нормалізований SQL
-- S2: у індексі є trip_number, щоб ORDER BY planned_start DESC, trip_number DESC читався з індексу
-- (без нього PG робить Incremental Sort, а Mongo повний SORT)
CREATE INDEX ix_trips_driver_start ON bench.trips (driver_id, planned_start DESC, trip_number DESC);
CREATE INDEX ix_trips_start        ON bench.trips (planned_start);
CREATE INDEX ix_trips_customer     ON bench.trips (customer_id);
CREATE INDEX ix_trips_vehicle      ON bench.trips (vehicle_id);
CREATE INDEX ix_fuel_filled        ON bench.fuel_transactions (filled_at);
CREATE INDEX ix_cargo_adr          ON bench.trip_cargo_items (adr_class, trip_id) WHERE kind = 'adr';

-- JSONB
CREATE INDEX ix_docs_driver_start ON bench.trip_docs (((doc->'driver'->>'id')::int), (doc->>'planned_start') DESC, trip_number DESC);
CREATE INDEX ix_docs_start        ON bench.trip_docs ((doc->>'planned_start'));
CREATE INDEX ix_docs_customer     ON bench.trip_docs (((doc->'customer'->>'id')::int));
CREATE INDEX gx_docs_cargo        ON bench.trip_docs USING gin ((doc->'cargo') jsonb_path_ops);
