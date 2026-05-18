"""Phase 1: MonoFS + MinIO storage bootstrap."""
from __future__ import annotations
import base64
import os
import secrets
import subprocess
from pathlib import Path
from string import Template

from stratatools.util import info, run, TEMPLATES, ROOT

NAMESPACE = os.environ.get("MONOFS_NAMESPACE", "monofs")
EXTERNAL_SERVICE_TYPE = os.environ.get("EXTERNAL_SERVICE_TYPE", "LoadBalancer")
MINIO_PVC_SIZE = os.environ.get("MINIO_PVC_SIZE", "50Gi")
FETCHER_PVC_SIZE = os.environ.get("FETCHER_PVC_SIZE", "20Gi")
SEARCH_PVC_SIZE = os.environ.get("SEARCH_PVC_SIZE", "40Gi")
NODE_PVC_SIZE = os.environ.get("NODE_PVC_SIZE", "100Gi")
MONOFS_SERVER_IMAGE = os.environ.get("MONOFS_SERVER_IMAGE", "monofs-server:latest")
MONOFS_ROUTER_IMAGE = os.environ.get("MONOFS_ROUTER_IMAGE", "monofs-router:latest")
MONOFS_FETCHER_IMAGE = os.environ.get("MONOFS_FETCHER_IMAGE", "monofs-fetcher:latest")
MONOFS_SEARCH_IMAGE = os.environ.get("MONOFS_SEARCH_IMAGE", "monofs-search:latest")
MINIO_IMAGE = os.environ.get("MINIO_IMAGE", "mirror.gcr.io/minio/minio:latest")
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", str(ROOT.parent / "monofs")))


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _vars() -> dict:
    monofs_token = os.environ.get("MONOFS_TOKEN") or secrets.token_urlsafe(32)
    monofs_encryption_key = os.environ.get(
        "MONOFS_ENCRYPTION_KEY",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    minio_ak = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
    minio_sk = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
    return {
        "NAMESPACE": NAMESPACE,
        "EXTERNAL_SERVICE_TYPE": EXTERNAL_SERVICE_TYPE,
        "MINIO_PVC_SIZE": MINIO_PVC_SIZE,
        "FETCHER_PVC_SIZE": FETCHER_PVC_SIZE,
        "SEARCH_PVC_SIZE": SEARCH_PVC_SIZE,
        "NODE_PVC_SIZE": NODE_PVC_SIZE,
        "MONOFS_SERVER_IMAGE": MONOFS_SERVER_IMAGE,
        "MONOFS_ROUTER_IMAGE": MONOFS_ROUTER_IMAGE,
        "MONOFS_FETCHER_IMAGE": MONOFS_FETCHER_IMAGE,
        "MONOFS_SEARCH_IMAGE": MONOFS_SEARCH_IMAGE,
        "MINIO_IMAGE": MINIO_IMAGE,
        "MONOFS_TOKEN": _b64(monofs_token),
        "MONOFS_ENCRYPTION_KEY": _b64(monofs_encryption_key),
        "MINIO_ACCESS_KEY": _b64(minio_ak),
        "MINIO_SECRET_KEY": _b64(minio_sk),
        "SUFFIX": "",
    }


def _render(name: str, extra: dict | None = None) -> str:
    v = _vars()
    if extra:
        v.update(extra)
    text = (TEMPLATES / "storage" / name).read_text()
    return Template(text).substitute(v)


def _apply(yaml_text: str, dry_run: bool) -> None:
    info(f"+ kubectl apply -f - (<<< {len(yaml_text)} bytes)")
    if dry_run:
        info(yaml_text[:200] + ("..." if len(yaml_text) > 200 else ""))
        return
    subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=yaml_text, text=True, check=True
    )


def build_images(dry_run: bool) -> None:
    for target, tag in [
        ("server", MONOFS_SERVER_IMAGE),
        ("router", MONOFS_ROUTER_IMAGE),
        ("fetcher", MONOFS_FETCHER_IMAGE),
        ("search", MONOFS_SEARCH_IMAGE),
    ]:
        run(
            ["docker", "build", "-t", tag, "--target", target, str(MONOFS_REPO_DIR)],
            check=True,
            dry_run=dry_run,
        )


_DEPLOYS = [
    "minio",
    "fetcher-a",
    "fetcher-b",
    "search-index",
    "node-a",
    "node-b",
    "node-c",
    "node-d",
    "node-e",
    "router-a",
    "router-b",
    "monofs-haproxy",
]


def deploy(dry_run: bool) -> None:
    info(f"=== deploying storage to namespace {NAMESPACE} ===")
    _apply(_render("namespace.yaml"), dry_run)
    _apply(_render("secret.yaml"), dry_run)
    _apply(_render("configmap-haproxy.yaml"), dry_run)
    _apply(_render("configmap-fetcher-s3.yaml"), dry_run)
    _apply(_render("pvc-minio.yaml"), dry_run)
    for s in ("a", "b"):
        _apply(_render("pvc-fetcher.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("pvc-search-index.yaml"), dry_run)
    for s in ("a", "b", "c", "d", "e"):
        _apply(_render("pvc-node.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("deploy-minio.yaml"), dry_run)
    for s in ("a", "b"):
        _apply(_render("deploy-fetcher.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("deploy-search-index.yaml"), dry_run)
    for s in ("a", "b", "c", "d", "e"):
        _apply(_render("deploy-node.yaml", {"SUFFIX": s}), dry_run)
    for s in ("a", "b"):
        _apply(_render("deploy-router.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("deploy-haproxy.yaml"), dry_run)
    _apply(_render("svc-minio.yaml"), dry_run)
    for s in ("a", "b"):
        _apply(_render("svc-fetcher.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("svc-search-index.yaml"), dry_run)
    for s in ("a", "b", "c", "d", "e"):
        _apply(_render("svc-node.yaml", {"SUFFIX": s}), dry_run)
    for s in ("a", "b"):
        _apply(_render("svc-router.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("svc-haproxy.yaml"), dry_run)
    _wait_rollouts(dry_run)


def _wait_rollouts(dry_run: bool) -> None:
    for d in _DEPLOYS:
        run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "rollout",
                "status",
                f"deployment/{d}",
                "--timeout=120s",
            ],
            check=False,
            dry_run=dry_run,
        )


def rollout(dry_run: bool) -> None:
    run(
        ["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment"],
        check=False,
        dry_run=dry_run,
    )
    _wait_rollouts(dry_run)


def stop(dry_run: bool) -> None:
    run(
        ["kubectl", "-n", NAMESPACE, "scale", "deployment", "--all", "--replicas=0"],
        check=False,
        dry_run=dry_run,
    )


def destroy(dry_run: bool) -> None:
    run(
        ["kubectl", "delete", "namespace", NAMESPACE, "--ignore-not-found"],
        check=False,
        dry_run=dry_run,
    )
