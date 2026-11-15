"""Image hardening (ticket 61): non-root unprivileged nginx on one consistent port, and a pinned uv build image."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PORT = 8080


def test_frontend_image_is_unprivileged_and_listens_on_the_same_port_everywhere() -> None:
    dockerfile = (ROOT / "frontend/Dockerfile.prod").read_text()
    assert re.search(r"^FROM nginxinc/nginx-unprivileged:\d+\.\d+-alpine\s*$", dockerfile, re.M)
    assert f"EXPOSE {PORT}" in dockerfile
    assert f"listen {PORT};" in (ROOT / "frontend/nginx.conf.template").read_text()
    compose = yaml.safe_load((ROOT / "docker-compose.image.yml").read_text())
    assert compose["services"]["frontend"]["ports"][0].endswith(f":{PORT}")
    docs = list(yaml.safe_load_all((ROOT / "k8s/base/frontend/deployment.yaml").read_text()))
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["ports"][0]["containerPort"] == PORT
    assert container["readinessProbe"]["httpGet"]["port"] == PORT
    service = next(d for d in docs if d["kind"] == "Service")
    assert service["spec"]["ports"][0] == {
        "port": 80,
        "targetPort": PORT,
    }  # Ingresses keep talking to port 80


def test_backend_image_pins_the_uv_version() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    match = re.search(r"COPY --from=ghcr\.io/astral-sh/uv:(\S+) ", dockerfile)
    assert match and re.fullmatch(r"\d+\.\d+\.\d+", match.group(1)), (
        "pin uv to an exact version, not :latest"
    )
