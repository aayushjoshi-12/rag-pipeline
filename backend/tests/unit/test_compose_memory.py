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


def _chroma_images() -> dict[str, str]:
    services = _compose()
    k8s = yaml.safe_load((ROOT / "k8s" / "base" / "chroma" / "deployment.yaml").read_text().split("\n---")[0])
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    ci_image = next(job["services"]["chroma"]["image"] for job in ci["jobs"].values() if "chroma" in job.get("services", {}))
    return {
        "compose chroma": services["chroma"]["image"],
        "compose chroma-test": services["chroma-test"]["image"],
        "k8s base": k8s["spec"]["template"]["spec"]["containers"][0]["image"],
        "ci": ci_image,
    }


def test_chroma_image_is_pinned_and_identical_everywhere() -> None:
    # The memory behaviour is version dependent (ticket 48); `latest` silently changed it under us (ticket 52).
    images = _chroma_images()
    assert len(set(images.values())) == 1, images
    image = next(iter(images.values()))
    tag = image.partition(":")[2]
    assert image.startswith("chromadb/chroma:") and tag not in ("", "latest"), image
    assert tag in (ROOT / "chroma" / "config.yaml").read_text()  # the pin is recorded next to the config


def test_beat_has_a_memory_limit_above_its_measured_peak() -> None:
    # celery beat imports every task module and idles at ~176 MiB (peak ~180 MiB cold start, ticket 52).
    beat = _compose()["beat"]
    assert beat["mem_limit"] == "384m" and beat["mem_reservation"] == "192m"


def test_chroma_test_service_is_a_throwaway_on_its_own_port() -> None:
    # Ticket 50: `make check` runs the Chroma tests against this, never against the dev Chroma and its volume.
    svc, dev = _compose()["chroma-test"], _compose()["chroma"]
    assert svc["profiles"] == ["test"] and svc["ports"] == ["8002:8000"] and svc["ports"] != dev["ports"]
    assert svc["tmpfs"] == ["/data"] and not any("chroma-data" in v for v in svc["volumes"])
    assert "./chroma/config.yaml:/config.yaml:ro" in svc["volumes"] and svc["mem_limit"]
