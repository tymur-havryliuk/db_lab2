// Схема графа: унікальні id для кожного label (constraint створює і індекс) та додаткові індекси.
// Виконується в neo4j_load.py, оператори розділені крапкою з комою.

CREATE CONSTRAINT city_id IF NOT EXISTS FOR (n:City) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT driver_id IF NOT EXISTS FOR (n:Driver) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT customer_id IF NOT EXISTS FOR (n:Customer) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT vehicle_id IF NOT EXISTS FOR (n:Vehicle) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT trip_id IF NOT EXISTS FOR (n:Trip) REQUIRE n.id IS UNIQUE;

// Пошук міста за назвою (початок і кінець шляху в запитах)
CREATE INDEX city_name IF NOT EXISTS FOR (n:City) ON (n.name);
// Номер рейсу і статус
CREATE INDEX trip_number IF NOT EXISTS FOR (n:Trip) ON (n.number);
CREATE INDEX trip_status IF NOT EXISTS FOR (n:Trip) ON (n.status);
