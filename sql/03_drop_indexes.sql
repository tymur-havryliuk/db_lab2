-- Видалення вторинних індексів перед завантаженням даних.
DROP INDEX IF EXISTS bench.ix_trips_driver_start;
DROP INDEX IF EXISTS bench.ix_trips_start;
DROP INDEX IF EXISTS bench.ix_trips_customer;
DROP INDEX IF EXISTS bench.ix_trips_vehicle;
DROP INDEX IF EXISTS bench.ix_fuel_filled;
DROP INDEX IF EXISTS bench.ix_cargo_adr;
DROP INDEX IF EXISTS bench.ix_docs_driver_start;
DROP INDEX IF EXISTS bench.ix_docs_start;
DROP INDEX IF EXISTS bench.ix_docs_customer;
DROP INDEX IF EXISTS bench.gx_docs_cargo;
