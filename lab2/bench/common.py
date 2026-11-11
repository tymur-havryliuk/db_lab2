"""Спільне для бенчмарку: масштаби, статистика, запис CSV, холодний кеш."""
from __future__ import annotations

import csv
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"

SCALES = {"10k": 10_000, "100k": 100_000, "500k": 500_000, "1m": 1_000_000}
VARIANTS = ("sql", "jsonb", "mongo")
SERVICE = {"sql": "postgres", "jsonb": "postgres", "mongo": "mongo"}
CONTAINER = {"postgres": "lab2-postgres", "mongo": "lab2-mongo"}


def summarize(us: Iterable[float]) -> dict[str, float]:
    """Медіана і хвости (p95, p99). Середнє не рахуємо: у латентності довгий правий хвіст."""
    a = np.asarray(list(us), dtype=float)
    if a.size == 0:
        return {"n": 0}
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return {"n": int(a.size), "p50_ms": p50 / 1000, "p95_ms": p95 / 1000, "p99_ms": p99 / 1000,
            "min_ms": a.min() / 1000, "max_ms": a.max() / 1000}


class CsvSink:
    """Дописує рядки в CSV, заголовок пише для нового файлу."""

    def __init__(self, path: Path, fields: list[str]):
        self.path, self.fields = path, fields
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        self._f = path.open("a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=fields, extrasaction="ignore")
        if new:
            self._w.writeheader()

    def write(self, row: dict[str, Any]) -> None:
        self._w.writerow(row)

    def writemany(self, rows: Iterable[dict[str, Any]]) -> None:
        self._w.writerows(rows)
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT).stdout.strip()


def wait_healthy(container: str, timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _sh("docker", "inspect", "-f", "{{.State.Health.Status}}", container) == "healthy":
            return
        time.sleep(0.5)
    raise TimeoutError(f"{container} не став healthy за {timeout}s")


def cold_restart(service: str) -> float:
    """Зупиняє СУБД, скидає page cache в Docker VM і запускає знову. Повертає час рестарту (с).

    Без drop_caches дані лишаються в page cache ОС, і холодний замір читав би з RAM.
    """
    t = time.time()
    _sh("docker", "compose", "stop", service)
    _sh("docker", "run", "--rm", "--privileged", "alpine:3.20", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches")
    _sh("docker", "compose", "start", service)
    wait_healthy(CONTAINER[service])
    return time.time() - t


def environment_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        info["docker"] = _sh("docker", "version", "--format", "{{.Server.Version}}")
        for c in CONTAINER.values():
            info[c] = _sh("docker", "inspect", "-f", "{{.Config.Image}} cpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}", c)
    except Exception as e:
        info["error"] = repr(e)
    return info
