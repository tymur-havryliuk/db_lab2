#!/usr/bin/env bash
# Повний прогін: для кожного масштабу завантаження, тест еквівалентності, бенчмарк.
# Вивід кожного кроку зберігається в results/<scale>/*.log.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --wait postgres mongo
docker compose --profile graph stop neo4j >/dev/null 2>&1 || true   # Neo4j не має займати ресурси під час замірів
for scale in ${SCALES:-10k 100k 1m}; do
  mkdir -p "results/$scale"
  echo "=== $scale: load $(date +%T)"
  uv run python -m lab2.load --scale "$scale" > "results/$scale/load.log" 2>&1
  tail -3 "results/$scale/load.log"
  echo "=== $scale: pytest $(date +%T)"
  uv run pytest -q > "results/$scale/pytest.log" 2>&1; tail -1 "results/$scale/pytest.log"
  # перезавантажувати дані перед бенчмарком не треба: check_writes видаляє свої рейси і повертає назви замовників.
  echo "=== $scale: bench $(date +%T)"
  uv run python -m lab2.bench run --scale "$scale" > "results/$scale/bench.log" 2>&1
  tail -1 "results/$scale/bench.log"
done
echo "=== done $(date +%T)"
