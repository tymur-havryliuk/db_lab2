// Демо-запити Neo4j (виконує lab2.neo4j_demo, кожен оператор закінчується ';').
// Формат: `// ### назва` починає розділ, коментарі перед оператором це його пояснення,
// `// @param ім'я = значення` задає параметр оператора (JSON або рядок).

// ### CRUD: створення вузлів і зв'язків
// CREATE додає вузол Customer з тимчасовим id = -1 (з demo:true, потім видаляється).
CREATE (c:Customer {id: -1, name: 'DEMO Замовник', tax_code: '00000000', demo: true})
RETURN c.id AS id, c.name AS name, c.demo AS demo;
// Тимчасове місто, щоб не чіпати реальні дороги: MATCH знаходить Київ, CREATE додає вузол і ребро ROAD.
MATCH (kyiv:City {name: 'Київ'})
CREATE (kyiv)-[r:ROAD {km: 10.0, hours: 1.0, demo: true}]->(d:City {id: -1, name: 'DEMO-місто', oblast: 'Київська', demo: true})
RETURN kyiv.name AS від, d.name AS до, r.km AS km, r.hours AS hours;
// Зв'язок замовник -> місто: MATCH обох кінців, CREATE ребра LOCATED_IN.
MATCH (c:Customer {id: -1}), (d:City {id: -1})
CREATE (c)-[:LOCATED_IN]->(d)
RETURN c.name AS замовник, d.name AS місто;

// ### CRUD: читання
// Читання вузла з усіма вихідними зв'язками (type() і labels() показують тип ребра і мітку сусіда).
MATCH (c:Customer {id: -1})-[r]->(x)
RETURN c.name AS замовник, type(r) AS зв_язок, labels(x)[0] AS мітка, x.name AS сусід;
// Читання тимчасового ребра ROAD (напрямок не важливий).
MATCH (:City {name: 'Київ'})-[r:ROAD]-(d:City {id: -1})
RETURN d.name AS місто, r.km AS km, r.hours AS hours;

// ### CRUD: оновлення
// SET змінює властивості вузла, RETURN показує новий стан.
MATCH (c:Customer {id: -1})
SET c.name = 'DEMO Замовник (оновлено)', c.tax_code = '99999999'
RETURN c.name AS name, c.tax_code AS tax_code;
// SET на ребрі: змінюємо відстань і час у дорозі.
MATCH (:City {name: 'Київ'})-[r:ROAD]-(:City {id: -1})
SET r.km = 25.5, r.hours = 0.5
RETURN r.km AS km, r.hours AS hours;
// Оновлення зв'язку це видалення старого ребра і створення нового: переносимо замовника в Київ.
MATCH (c:Customer {id: -1})-[old:LOCATED_IN]->(:City)
DELETE old
WITH c
MATCH (k:City {name: 'Київ'})
CREATE (c)-[:LOCATED_IN]->(k)
RETURN c.name AS замовник, k.name AS нове_місто;

// ### CRUD: видалення
// DELETE видаляє тільки ребро, вузли лишаються.
MATCH (:Customer {id: -1})-[r:LOCATED_IN]->(:City)
DELETE r
RETURN count(r) AS видалено_ребер;
// DETACH DELETE видаляє вузол разом з усіма ребрами. Усі тимчасові вузли мають demo:true.
MATCH (n {demo: true})
DETACH DELETE n
RETURN count(n) AS видалено_вузлів;
// Перевірка: тимчасових вузлів не лишилось.
MATCH (n {demo: true})
RETURN count(n) AS залишилось_demo_вузлів;

// ### Найкоротший шлях за кількістю переїздів
// Вбудований shortestPath (BFS) по ребрах ROAD без напрямку, довжина це кількість ребер.
// @param from = Київ
// @param to = Одеса
MATCH (a:City {name: $from}), (b:City {name: $to})
MATCH p = shortestPath((a)-[:ROAD*..10]-(b))
RETURN [n IN nodes(p) | n.name] AS маршрут, length(p) AS переїздів,
       reduce(s = 0.0, r IN relationships(p) | s + r.km) AS км_разом;

// ### Найкоротший шлях за кілометрами (власний Cypher, без GDS/APOC)
// Перебираємо всі прості шляхи довжиною 1..6, сумуємо km через reduce і беремо мінімум (ORDER BY ... LIMIT 1).
// Це повний перебір, тому глибина обмежена, і довші шляхи не знайдуться. Для великих графів потрібен Dijkstra з GDS.
// @param from = Київ
// @param to = Одеса
MATCH (a:City {name: $from}), (b:City {name: $to})
MATCH p = (a)-[:ROAD*1..6]-(b)
WITH p, reduce(s = 0.0, r IN relationships(p) | s + r.km) AS km
ORDER BY km ASC
LIMIT 1
RETURN [n IN nodes(p) | n.name] AS маршрут, length(p) AS переїздів, km AS км_разом;

// ### Досяжні міста за ≤ 2 переїздів
// Перебір шляхів довжиною 1..2 від міста, min(length(p)) дає найменшу кількість переїздів до кожного міста.
// @param from = Ужгород
MATCH p = (a:City {name: $from})-[:ROAD*1..2]-(b:City)
WHERE b <> a
WITH b, min(length(p)) AS hops
RETURN hops AS переїздів, count(b) AS міст, collect(b.name)[..5] AS приклади
ORDER BY hops;

// ### Водії зі спільними замовниками
// Беремо водія з найбільшою кількістю рейсів і шукаємо інших водіїв через спільного Customer
// (Driver-DROVE->Trip-FOR->Customer<-FOR-Trip<-DROVE-Driver) і рахуємо різних спільних замовників.
MATCH (d1:Driver)-[:DROVE]->(t1:Trip)
WITH d1, count(t1) AS рейсів
ORDER BY рейсів DESC, d1.id
LIMIT 1
MATCH (d1)-[:DROVE]->(:Trip)-[:FOR]->(c:Customer)<-[:FOR]-(:Trip)<-[:DROVE]-(d2:Driver)
WHERE d2 <> d1
RETURN d1.full_name AS водій, d2.full_name AS колега, count(DISTINCT c) AS спільних_замовників
ORDER BY спільних_замовників DESC, колега
LIMIT 10;

// ### Топ замовників за кількістю рейсів
// Групування за замовником (count і sum по ребрах FOR), сортування за кількістю рейсів.
MATCH (c:Customer)<-[:FOR]-(t:Trip)
RETURN c.id AS id, c.name AS замовник, count(t) AS рейсів, round(sum(t.freight_price)) AS вантажоперевезень_грн
ORDER BY рейсів DESC, c.id
LIMIT 10;
