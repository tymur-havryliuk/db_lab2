-- Нормалізована схема для бенчмарку.
-- Тригерів change_log/аудиту тут немає: вони сповільнюють запис у SQL
-- і порівняння з Mongo та JSONB було б нерівним.
DROP SCHEMA IF EXISTS bench CASCADE;
CREATE SCHEMA bench;

CREATE TYPE bench.trip_status     AS ENUM ('planned', 'in_progress', 'completed', 'cancelled');
CREATE TYPE bench.cargo_kind      AS ENUM ('general', 'reefer', 'adr', 'oversize');
CREATE TYPE bench.trip_event_type AS ENUM ('departed', 'checkpoint', 'fuel', 'idle', 'incident', 'delivered');
CREATE TYPE bench.incident_type   AS ENUM ('accident', 'fine', 'breakdown', 'damage');

CREATE TABLE bench.cities (
    city_id int PRIMARY KEY,
    name    varchar(100) NOT NULL,
    oblast  varchar(100)
);

CREATE TABLE bench.routes (
    route_id       int PRIMARY KEY,
    code           varchar(30) NOT NULL UNIQUE,
    from_city_id   int NOT NULL REFERENCES bench.cities(city_id),
    to_city_id     int NOT NULL REFERENCES bench.cities(city_id),
    distance_km    numeric(8,1) NOT NULL,
    duration_hours numeric(5,1) NOT NULL,
    CHECK (from_city_id <> to_city_id)
);

CREATE TABLE bench.vehicles (
    vehicle_id   int PRIMARY KEY,
    vin          char(17)    NOT NULL UNIQUE,
    plate_number varchar(8)  NOT NULL UNIQUE,
    model        varchar(80) NOT NULL
);

CREATE TABLE bench.drivers (
    driver_id      int PRIMARY KEY,
    last_name      varchar(60) NOT NULL,
    first_name     varchar(60) NOT NULL,
    middle_name    varchar(60),
    license_number varchar(9)  NOT NULL UNIQUE,
    phone          varchar(20)
);

CREATE TABLE bench.customers (
    customer_id int PRIMARY KEY,
    name        varchar(200) NOT NULL,
    tax_code    varchar(10),
    city_id     int REFERENCES bench.cities(city_id)
);

CREATE TABLE bench.trips (
    trip_id        bigint PRIMARY KEY,           -- задає генератор, не identity
    trip_number    varchar(20) NOT NULL UNIQUE,
    vehicle_id     int REFERENCES bench.vehicles(vehicle_id),
    driver_id      int REFERENCES bench.drivers(driver_id),
    route_id       int REFERENCES bench.routes(route_id),
    customer_id    int NOT NULL REFERENCES bench.customers(customer_id),
    status         bench.trip_status NOT NULL,
    planned_start  timestamptz NOT NULL,
    planned_end    timestamptz NOT NULL,
    started_at     timestamptz,
    finished_at    timestamptz,
    odometer_start int,
    odometer_end   int,
    distance_km    int GENERATED ALWAYS AS (odometer_end - odometer_start) STORED,
    freight_price  numeric(12,2) NOT NULL,
    cancel_reason  varchar(255)
);

CREATE TABLE bench.trip_cargo_items (
    trip_id       bigint   NOT NULL REFERENCES bench.trips(trip_id) ON DELETE CASCADE,
    line_no       smallint NOT NULL,
    kind          bench.cargo_kind NOT NULL,
    title         varchar(200) NOT NULL,
    weight_kg     int NOT NULL,
    packages      int,
    temp_min      smallint,
    temp_max      smallint,
    un_number     char(4),
    adr_class     smallint,
    packing_group varchar(3),
    length_cm     int,
    width_cm      int,
    height_cm     int,
    PRIMARY KEY (trip_id, line_no),
    CONSTRAINT ck_cargo_kind_cols CHECK (
        CASE kind
            WHEN 'general' THEN
                packages IS NOT NULL
                AND temp_min IS NULL AND temp_max IS NULL
                AND un_number IS NULL AND adr_class IS NULL AND packing_group IS NULL
                AND length_cm IS NULL AND width_cm IS NULL AND height_cm IS NULL
            WHEN 'reefer' THEN
                temp_min IS NOT NULL AND temp_max IS NOT NULL AND temp_min < temp_max
                AND packages IS NULL
                AND un_number IS NULL AND adr_class IS NULL AND packing_group IS NULL
                AND length_cm IS NULL AND width_cm IS NULL AND height_cm IS NULL
            WHEN 'adr' THEN
                un_number IS NOT NULL AND adr_class IS NOT NULL AND packing_group IS NOT NULL
                AND adr_class BETWEEN 1 AND 9 AND packing_group IN ('I', 'II', 'III')
                AND packages IS NULL AND temp_min IS NULL AND temp_max IS NULL
                AND length_cm IS NULL AND width_cm IS NULL AND height_cm IS NULL
            WHEN 'oversize' THEN
                length_cm IS NOT NULL AND width_cm IS NOT NULL AND height_cm IS NOT NULL
                AND packages IS NULL AND temp_min IS NULL AND temp_max IS NULL
                AND un_number IS NULL AND adr_class IS NULL AND packing_group IS NULL
        END
    )
);

CREATE TABLE bench.trip_events (
    trip_id       bigint NOT NULL REFERENCES bench.trips(trip_id) ON DELETE CASCADE,
    seq           int    NOT NULL,
    type          bench.trip_event_type NOT NULL,
    occurred_at   timestamptz NOT NULL,
    odometer_km   int,
    location      varchar(150),
    duration_min  int,
    reason        varchar(200),
    receiver_name varchar(150),
    PRIMARY KEY (trip_id, seq)
);

CREATE TABLE bench.fuel_transactions (
    fuel_tx_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id     bigint NOT NULL,
    event_seq   int    NOT NULL,
    vehicle_id  int REFERENCES bench.vehicles(vehicle_id),
    driver_id   int REFERENCES bench.drivers(driver_id),
    filled_at   timestamptz NOT NULL,
    quantity    numeric(8,2) NOT NULL,
    unit_price  numeric(8,2) NOT NULL,
    total_cost  numeric(12,2) GENERATED ALWAYS AS (round(quantity * unit_price, 2)) STORED,
    odometer_km int NOT NULL,
    station     varchar(150),
    FOREIGN KEY (trip_id, event_seq) REFERENCES bench.trip_events(trip_id, seq) ON DELETE CASCADE,
    UNIQUE (trip_id, event_seq)
);

CREATE TABLE bench.incidents (
    incident_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id         bigint NOT NULL,
    event_seq       int    NOT NULL,
    vehicle_id      int REFERENCES bench.vehicles(vehicle_id),
    driver_id       int REFERENCES bench.drivers(driver_id),
    type            bench.incident_type NOT NULL,
    occurred_at     timestamptz NOT NULL,
    description     text NOT NULL,
    amount          numeric(14,2) NOT NULL,
    driver_at_fault boolean NOT NULL,
    FOREIGN KEY (trip_id, event_seq) REFERENCES bench.trip_events(trip_id, seq) ON DELETE CASCADE,
    UNIQUE (trip_id, event_seq)
);

-- JSONB-варіант: той самий документ е-ТТН
CREATE TABLE bench.trip_docs (
    trip_number text PRIMARY KEY,
    doc         jsonb NOT NULL
);
