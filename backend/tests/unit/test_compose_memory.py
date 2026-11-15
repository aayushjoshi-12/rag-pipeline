"""docker-compose.yml memory guards and the worker concurrency cap (tickets 48/49). No Docker needed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from app.celery_app import celery_app
from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded["services"]


def test_chroma_api_and_worker_have_memory_limits() -> None:
    # Without a limit the VM's OOM killer picks the victim (it killed chroma at 5.7 GB and the api at 3.3 GB).
    services = _compose()
    for name in ("chroma", "api", "worker"):
        assert services[name].get("mem_limit"), f"{name} has no mem_limit"
        assert services[name].get("mem_reservation"), f"{name} has no mem_reservation"


def test_chroma_compose_uses_the_bounded_index_cache_config() -> None:
    volumes = _compose()["chroma"]["volumes"]
    assert "./chroma/config.yaml:/config.yaml:ro" in volumes
    cfg = yaml.safe_load((ROOT / "chroma" / "config.yaml").read_text())
    assert cfg["persist_path"] == "/data" and "chroma-data:/data" in volumes
    assert cfg["segment_manager"]["hnsw_index_pool_cache_config"]["lru"]["capacity"] >= 1


def test_worker_concurrency_is_capped_not_one_child_per_host_cpu() -> None:
    # Celery defaults to os.cpu_count() children (10 on a laptop, the node's count in a pod); each idles at
    # ~100 MiB private memory, which put a worker at ~1.1 GiB.
    assert Settings().worker_concurrency == 2
    assert celery_app.conf.worker_concurrency == Settings().worker_concurrency
