# Лабораторна 2: NoSQL у системі управління автопарком

Гаврилюк Тимур, ТК-31. Звіт з результатами здається окремим PDF.

Електронна ТТН рейсу зберігається у трьох варіантах: документ у MongoDB, нормалізовані таблиці PostgreSQL (схема bench) і документ у PostgreSQL JSONB. Друга NoSQL-база: Neo4j (міста, дороги, водії, замовники, рейси).

## Запуск

Потрібні Docker і uv.

```bash
cp .env.example .env            # вписати свої паролі
docker compose up -d --wait postgres mongo

uv run python -m lab2.load --scale 10k          # генерація і завантаження (10k, 100k або 1m)
uv run pytest -q                                # перевірка, що всі варіанти повертають однакове
uv run python -m lab2.bench run --scale 10k     # бенчмарк
uv run python -m lab2.bench.ablation --scale 10k  # додаткові заміри
uv run python -m lab2.report                    # графіки в report/

docker compose --profile graph up -d --wait neo4j
uv run python -m lab2.neo4j_load --source pg
uv run python -m lab2.neo4j_demo
```

## Що де лежить

- `sql/`: схема bench, індекси, функція дописування події
- `mongo/`: валідатор і індекси колекції
- `neo4j/`: схема графа, запити Cypher і їхні SQL-аналоги
- `lab2/`: генератор даних, завантаження, репозиторії для трьох варіантів, бенчмарк, графіки
- `tests/`: перевірка однаковості результатів
- `results/`: CSV з замірами і плани виконання
- `report/`: графіки
