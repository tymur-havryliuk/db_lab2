-- SQL-еквіваленти графових запитів (схема bench) для порівняння з Cypher в lab2.graph_compare.
-- Формат: `-- ### ключ` починає запит, параметри мають вигляд %(ім'я)s (psycopg). Коментарі в рядки коду не рахуються.
-- Дороги без напрямку, як у графі: одне ребро на пару міст з мінімальним km (див. lab2/neo4j_load.py).
-- Рейси обмежені trip_id <= 10000, бо в графі 10 000 рейсів.

-- ### hops
-- Найкоротший шлях за кількістю переїздів Київ -> Одеса: перебір простих шляхів (масив відвіданих міст
-- проти циклів) глибиною до 6, обрив шляху в цільовому місті, мінімум за кількістю ребер.
WITH RECURSIVE edges AS (
    SELECT least(from_city_id, to_city_id) AS a, greatest(from_city_id, to_city_id) AS b,
           min(distance_km)::float8 AS km
    FROM bench.routes GROUP BY 1, 2
), ways AS (
    SELECT a, b, km FROM edges UNION ALL SELECT b, a, km FROM edges
), walk(city, path, hops, km) AS (
    SELECT city_id, ARRAY[city_id], 0, 0::float8 FROM bench.cities WHERE name = %(from)s
  UNION ALL
    SELECT w.b, walk.path || w.b, walk.hops + 1, walk.km + w.km
    FROM walk JOIN ways w ON w.a = walk.city
    WHERE walk.hops < 6 AND NOT w.b = ANY (walk.path)
      AND walk.city <> (SELECT city_id FROM bench.cities WHERE name = %(to)s)
)
SELECT hops, km, (SELECT array_agg(c.name ORDER BY u.ord)
                  FROM unnest(path) WITH ORDINALITY u(id, ord) JOIN bench.cities c ON c.city_id = u.id) AS path
FROM walk WHERE city = (SELECT city_id FROM bench.cities WHERE name = %(to)s)
ORDER BY hops, km LIMIT 1;

-- ### weighted
-- Зважений найкоротший шлях за км Київ -> Одеса, глибина <= 6: той самий перебір, мінімум за сумою km.
WITH RECURSIVE edges AS (
    SELECT least(from_city_id, to_city_id) AS a, greatest(from_city_id, to_city_id) AS b,
           min(distance_km)::float8 AS km
    FROM bench.routes GROUP BY 1, 2
), ways AS (
    SELECT a, b, km FROM edges UNION ALL SELECT b, a, km FROM edges
), walk(city, path, hops, km) AS (
    SELECT city_id, ARRAY[city_id], 0, 0::float8 FROM bench.cities WHERE name = %(from)s
  UNION ALL
    SELECT w.b, walk.path || w.b, walk.hops + 1, walk.km + w.km
    FROM walk JOIN ways w ON w.a = walk.city
    WHERE walk.hops < 6 AND NOT w.b = ANY (walk.path)
      AND walk.city <> (SELECT city_id FROM bench.cities WHERE name = %(to)s)
)
SELECT hops, km, (SELECT array_agg(c.name ORDER BY u.ord)
                  FROM unnest(path) WITH ORDINALITY u(id, ord) JOIN bench.cities c ON c.city_id = u.id) AS path
FROM walk WHERE city = (SELECT city_id FROM bench.cities WHERE name = %(to)s)
ORDER BY km LIMIT 1;

-- ### reachable
-- Досяжні міста за <= 2 переїздів з Ужгорода: перебір шляхів глибиною 2, мінімальна кількість переїздів на місто.
WITH RECURSIVE edges AS (
    SELECT least(from_city_id, to_city_id) AS a, greatest(from_city_id, to_city_id) AS b
    FROM bench.routes GROUP BY 1, 2
), ways AS (
    SELECT a, b FROM edges UNION ALL SELECT b, a FROM edges
), walk(city, path, hops) AS (
    SELECT city_id, ARRAY[city_id], 0 FROM bench.cities WHERE name = %(from)s
  UNION ALL
    SELECT w.b, walk.path || w.b, walk.hops + 1
    FROM walk JOIN ways w ON w.a = walk.city
    WHERE walk.hops < 2 AND NOT w.b = ANY (walk.path)
), best AS (
    SELECT city, min(hops) AS hops FROM walk WHERE hops > 0 GROUP BY city
)
SELECT hops, count(*) AS cities FROM best GROUP BY hops ORDER BY hops;

-- ### shared_drivers
-- Водії зі спільними замовниками: водій з найбільшою кількістю рейсів; пари (водій, замовник) дедуплікуються
-- до з'єднання (інакше самоз'єднання trips множить рядки), потім рахуємо спільних замовників.
WITH top AS (
    SELECT driver_id FROM bench.trips WHERE trip_id <= 10000
    GROUP BY driver_id ORDER BY count(*) DESC, driver_id LIMIT 1
), cust AS (
    SELECT DISTINCT t.customer_id FROM bench.trips t JOIN top USING (driver_id) WHERE t.trip_id <= 10000
), pairs AS (
    SELECT DISTINCT t.driver_id, t.customer_id
    FROM bench.trips t JOIN cust USING (customer_id)
    WHERE t.trip_id <= 10000 AND t.driver_id <> (SELECT driver_id FROM top)
)
SELECT concat_ws(' ', d.last_name, d.first_name, d.middle_name) AS colleague, count(*) AS shared
FROM pairs p JOIN bench.drivers d ON d.driver_id = p.driver_id
GROUP BY d.driver_id, d.last_name, d.first_name, d.middle_name
ORDER BY shared DESC, concat_ws(' ', d.last_name, d.first_name, d.middle_name) COLLATE "C"
LIMIT 10;

-- ### top_customers
-- Топ замовників за кількістю рейсів: GROUP BY customer_id.
SELECT c.customer_id AS id, c.name, count(*) AS trips, round(sum(t.freight_price)) AS freight
FROM bench.trips t JOIN bench.customers c ON c.customer_id = t.customer_id
WHERE t.trip_id <= 10000
GROUP BY c.customer_id, c.name
ORDER BY trips DESC, c.customer_id
LIMIT 10;
