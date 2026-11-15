"""Structural validation of the rendered Kustomize overlays (no cluster needed).

Catches the mistakes that only show up at deploy time: dangling ConfigMap/Secret/PVC/Service
references, selector/label drift, wrong Service ports, ConfigMap keys the app doesn't know.
Skipped when kubectl isn't installed. This does NOT prove the manifests apply to a real cluster.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]
OVERLAYS = ["dev", "prod"]
pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")


def render(overlay: str) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "k8s" / "overlays" / overlay)],
        capture_output=True, text=True, check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def pod_specs(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs = {}
    for kind in ("Deployment", "Job"):
        for name, d in by_kind(docs, kind).items():
            specs[name] = d["spec"]["template"]
    return specs


@pytest.fixture(params=OVERLAYS)
def docs(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    return render(request.param)


def test_everything_is_in_the_app_namespace(docs: list[dict[str, Any]]) -> None:
    for d in docs:
        if d["kind"] != "Namespace":
            assert d["metadata"].get("namespace") == "rag-pipeline", (d["kind"], d["metadata"]["name"])


def test_deployment_selectors_match_template_labels(docs: list[dict[str, Any]]) -> None:
    for name, d in by_kind(docs, "Deployment").items():
        sel = d["spec"]["selector"]["matchLabels"]
        labels = d["spec"]["template"]["metadata"]["labels"]
        assert sel.items() <= labels.items(), name


def test_references_resolve(docs: list[dict[str, Any]]) -> None:
    cms, secrets, pvcs = by_kind(docs, "ConfigMap"), by_kind(docs, "Secret"), by_kind(docs, "PersistentVolumeClaim")
    for name, tpl in pod_specs(docs).items():
        spec = tpl["spec"]
        for vol in spec.get("volumes", []):
            claim = vol.get("persistentVolumeClaim", {}).get("claimName")
            if claim:
                assert claim in pvcs, f"{name}: missing PVC {claim}"
        for c in spec["containers"]:
            for ef in c.get("envFrom", []):
                if "configMapRef" in ef:
                    assert ef["configMapRef"]["name"] in cms, f"{name}: missing ConfigMap"
                if "secretRef" in ef:
                    assert ef["secretRef"]["name"] in secrets, f"{name}: missing Secret"
            for env in c.get("env", []):
                ref = env.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    secret = secrets[ref["name"]]
                    keys = set(secret.get("stringData", {})) | set(secret.get("data", {}))
                    assert ref["key"] in keys, f"{name}: secret key {ref['key']} missing"
            mounts = {m["name"] for m in c.get("volumeMounts", [])}
            assert mounts <= {v["name"] for v in spec.get("volumes", [])}, f"{name}: mount without volume"


def test_services_select_pods_and_target_real_ports(docs: list[dict[str, Any]]) -> None:
    deployments = by_kind(docs, "Deployment")
    for name, svc in by_kind(docs, "Service").items():
        sel = svc["spec"]["selector"]
        matches = [
            d for d in deployments.values() if sel.items() <= d["spec"]["template"]["metadata"]["labels"].items()
        ]
        assert matches, f"service {name} selects nothing"
        ports = {p["containerPort"] for c in matches[0]["spec"]["template"]["spec"]["containers"] for p in c.get("ports", [])}
        for p in svc["spec"]["ports"]:
            assert p["targetPort"] in ports, f"service {name}: targetPort {p['targetPort']} not exposed"


def test_configmap_keys_are_known_settings(docs: list[dict[str, Any]]) -> None:
    fields = set(Settings.model_fields)
    for key in by_kind(docs, "ConfigMap")["rag-pipeline-config"]["data"]:
        assert key.lower() in fields, f"ConfigMap key {key} is not a Settings field (typo?)"


def test_cross_service_hostnames_exist(docs: list[dict[str, Any]]) -> None:
    services = by_kind(docs, "Service")
    cm = by_kind(docs, "ConfigMap")["rag-pipeline-config"]["data"]
    assert cm["CHROMA_HOST"] in services and int(cm["CHROMA_PORT"]) in {p["port"] for p in services[cm["CHROMA_HOST"]]["spec"]["ports"]}
    assert cm["REDIS_URL"].split("//")[1].split(":")[0] in services
    db_url = by_kind(docs, "Secret")["rag-pipeline-secrets"]["stringData"]["DATABASE_URL"]
    assert db_url.split("@")[1].split(":")[0] in services
    api_port = services["rag-pipeline-api"]["spec"]["ports"][0]["port"]
    fe = by_kind(docs, "Deployment")["rag-pipeline-frontend"]["spec"]["template"]["spec"]["containers"][0]["env"]
    upstream = next(e["value"] for e in fe if e["name"] == "API_UPSTREAM")
    host_port = upstream.removeprefix("http://")
    host, _, port = host_port.partition(":")
    assert host in services and int(port or 80) == api_port, f"API_UPSTREAM {upstream} does not match the api Service"


def test_db_password_comes_from_the_same_secret_as_database_url(docs: list[dict[str, Any]]) -> None:
    sec = by_kind(docs, "Secret")["rag-pipeline-secrets"]["stringData"]
    assert f":{sec['POSTGRES_PASSWORD']}@" in sec["DATABASE_URL"]


def test_singletons_and_security_context(docs: list[dict[str, Any]]) -> None:
    deployments = by_kind(docs, "Deployment")
    assert deployments["rag-pipeline-beat"]["spec"]["replicas"] == 1  # two beats double-fire tasks
    assert deployments["rag-pipeline-beat"]["spec"]["strategy"]["type"] == "Recreate"
    for name in ("rag-pipeline-api", "rag-pipeline-worker", "rag-pipeline-beat"):
        assert deployments[name]["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True


def test_hpa_targets_exist_and_bounds_sane() -> None:
    docs = render("prod")
    deployments = by_kind(docs, "Deployment")
    for name, hpa in by_kind(docs, "HorizontalPodAutoscaler").items():
        assert hpa["spec"]["scaleTargetRef"]["name"] in deployments, name
        assert hpa["spec"]["minReplicas"] <= hpa["spec"]["maxReplicas"]


def test_dev_overlay_includes_ollama_and_prod_has_ingress() -> None:
    assert "ollama" in by_kind(render("dev"), "Service")
    prod = render("prod")
    ing = by_kind(prod, "Ingress")["rag-pipeline"]
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] in by_kind(prod, "Service")


def test_service_links_are_disabled_everywhere(docs: list[dict[str, Any]]) -> None:
    # Found on a real cluster: a Service named `chroma` injects CHROMA_PORT=tcp://ip:8000 into pods
    # in the namespace, and the chroma server crashed parsing it as its own CHROMA_PORT setting.
    for name, tpl in pod_specs(docs).items():
        assert tpl["spec"].get("enableServiceLinks") is False, name


def test_chroma_volume_is_mounted_where_the_image_persists() -> None:
    # Found on a real cluster: chromadb/chroma persists to /data; a PVC mounted elsewhere looks fine
    # but every vector is lost when the pod restarts.
    for overlay in ("dev", "prod", "kind"):
        tpl = pod_specs(render(overlay))["chroma"]["spec"]["containers"][0]
        assert {m["mountPath"] for m in tpl["volumeMounts"]} == {"/data"}, overlay


def test_kind_overlay_is_self_consistent() -> None:
    docs = render("kind")
    # real secrets are created out of band, never overwritten by placeholders on apply
    assert "rag-pipeline-secrets" not in by_kind(docs, "Secret")
    assert "ollama" not in by_kind(docs, "Service")  # host Ollama is used instead
    cfg = by_kind(docs, "ConfigMap")["rag-pipeline-config"]["data"]
    assert cfg["OLLAMA_BASE_URL"] == "http://host.docker.internal:11434"
    pvc = by_kind(docs, "PersistentVolumeClaim")["rag-pipeline-uploads"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    for name, tpl in pod_specs(docs).items():
        for c in tpl["spec"]["containers"]:
            if c["image"].startswith("rag-pipeline/"):
                assert c["image"].endswith(":kind"), name


def test_headless_overlay_has_no_dashboard_and_routes_the_ingress_to_the_api() -> None:
    docs = render("headless")
    assert not [d for d in docs if "frontend" in d["metadata"]["name"]]
    api = by_kind(docs, "Deployment")
    assert {"rag-pipeline-api", "rag-pipeline-worker", "rag-pipeline-beat"} <= set(api)
    backends = [
        p["backend"]["service"]["name"]
        for ing in by_kind(docs, "Ingress").values()
        for rule in ing["spec"]["rules"]
        for p in rule["http"]["paths"]
    ]
    assert backends == ["rag-pipeline-api"] and "rag-pipeline-api" in by_kind(docs, "Service")


@pytest.mark.parametrize("overlay", ["dev", "prod", "kind", "headless"])
def test_api_waits_for_the_migration_before_it_seeds_auth(overlay: str) -> None:
    # Found on a real cluster: `kubectl apply -k` starts the api next to Postgres and the migrate Job. The api seeds
    # roles + the bootstrap admin once at startup and never retries, so an api that wins the race is Ready but nobody
    # can log in ("invalid credentials"). The pod must not start its main container before Alembic is at head.
    tpl = pod_specs(render(overlay))["rag-pipeline-api"]["spec"]
    waiters = [c for c in tpl.get("initContainers", []) if c["name"] == "wait-for-migrate"]
    assert len(waiters) == 1, overlay
    script = " ".join(waiters[0]["command"])
    assert "alembic current" in script and "(head)" in script
    assert {"configMapRef", "secretRef"} <= {k for ef in waiters[0]["envFrom"] for k in ef}  # DATABASE_URL reaches it
    assert waiters[0]["image"].startswith("rag-pipeline/backend:")  # same image (and tag) as the api



def test_s3_component_drops_the_shared_uploads_volume(tmp_path: Path) -> None:
    # Measured on a 2-worker-node kind cluster: with a ReadWriteOnce uploads PVC every api/worker pod is pinned to the
    # volume's node (a worker forced elsewhere stays Pending). STORAGE_BACKEND=s3 is the multi-node answer, so the
    # component must set the backend AND remove the PVC + its mounts' claims, or the pods would still be pinned.
    k8s = ROOT / "k8s"
    (tmp_path / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        f"resources: [{os.path.relpath(k8s / 'overlays' / 'prod', tmp_path)}]\n"
        f"components: [{os.path.relpath(k8s / 'components' / 's3', tmp_path)}]\n"
    )
    out = subprocess.run(["kubectl", "kustomize", str(tmp_path)], capture_output=True, text=True, check=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cfg = by_kind(docs, "ConfigMap")["rag-pipeline-config"]["data"]
    assert cfg["STORAGE_BACKEND"] == "s3" and cfg["S3_BUCKET"]
    assert "rag-pipeline-uploads" not in by_kind(docs, "PersistentVolumeClaim")
    for name in ("rag-pipeline-api", "rag-pipeline-worker"):
        vols = by_kind(docs, "Deployment")[name]["spec"]["template"]["spec"]["volumes"]
        assert vols == [{"name": "uploads", "emptyDir": {}}], name
    test_references_resolve(docs)  # no dangling claim / mount after the patches
