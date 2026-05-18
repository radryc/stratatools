"""Phase 2: Guardian control plane bootstrap."""
from __future__ import annotations
import base64
import os
import secrets
import subprocess
import time
from string import Template

import yaml

from stratatools.util import info, warn, run, TEMPLATES, PARTITIONS

NAMESPACE = os.environ.get("GUARDIAN_NAMESPACE", "guardian")
STORAGE_NAMESPACE = os.environ.get("MONOFS_NAMESPACE", "monofs")
EXTERNAL_SERVICE_TYPE = os.environ.get("EXTERNAL_SERVICE_TYPE", "LoadBalancer")
GUARDIAN_IMAGE = os.environ.get("GUARDIAN_IMAGE", "guardian:latest")
GUARDIAN_PUSHER_IMAGE = os.environ.get(
    "GUARDIAN_PUSHER_IMAGE", "guardian-pusher-k8s:latest"
)
GUARDIAN_MONOFS_ROUTER = os.environ.get(
    "GUARDIAN_MONOFS_ROUTER",
    f"monofs-external.{STORAGE_NAMESPACE}.svc.cluster.local:9090",
)
GUARDIAN_MONOFS_CLIENT_API_ENDPOINT = os.environ.get(
    "GUARDIAN_MONOFS_CLIENT_API_ENDPOINT", GUARDIAN_MONOFS_ROUTER
)
GUARDIAN_MONOFS_USE_EXTERNAL_ADDRESSES = os.environ.get(
    "GUARDIAN_MONOFS_USE_EXTERNAL_ADDRESSES", "false"
)
GUARDIAN_MONOFS_CLIENT_USE_EXTERNAL_ADDRESSES = os.environ.get(
    "GUARDIAN_MONOFS_CLIENT_USE_EXTERNAL_ADDRESSES", "true"
)
GUARDIAN_PUSHER_NAME = os.environ.get("GUARDIAN_PUSHER_NAME", "k8s-main")
GUARDIAN_CLUSTER = os.environ.get("GUARDIAN_CLUSTER", GUARDIAN_PUSHER_NAME)
GUARDIAN_PUSHERS = os.environ.get(
    "GUARDIAN_PUSHERS", f"{GUARDIAN_PUSHER_NAME}:/.queues/{GUARDIAN_PUSHER_NAME}"
)
GUARDIAN_UI_LISTEN = os.environ.get("GUARDIAN_UI_LISTEN", ":8080")
GUARDIAN_UI_BASE_URL = os.environ.get("GUARDIAN_UI_BASE_URL", "")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _vars() -> dict:
    monofs_token = os.environ.get("MONOFS_TOKEN") or secrets.token_urlsafe(32)
    cdt = os.environ.get("CLIENT_DISCOVERY_TOKEN") or secrets.token_urlsafe(32)
    return {
        "NAMESPACE": NAMESPACE,
        "STORAGE_NAMESPACE": STORAGE_NAMESPACE,
        "EXTERNAL_SERVICE_TYPE": EXTERNAL_SERVICE_TYPE,
        "GUARDIAN_IMAGE": GUARDIAN_IMAGE,
        "GUARDIAN_PUSHER_IMAGE": GUARDIAN_PUSHER_IMAGE,
        "GUARDIAN_MONOFS_ROUTER": GUARDIAN_MONOFS_ROUTER,
        "GUARDIAN_MONOFS_CLIENT_API_ENDPOINT": GUARDIAN_MONOFS_CLIENT_API_ENDPOINT,
        "GUARDIAN_MONOFS_USE_EXTERNAL_ADDRESSES": GUARDIAN_MONOFS_USE_EXTERNAL_ADDRESSES,
        "GUARDIAN_MONOFS_CLIENT_USE_EXTERNAL_ADDRESSES": GUARDIAN_MONOFS_CLIENT_USE_EXTERNAL_ADDRESSES,
        "GUARDIAN_PUSHER_NAME": GUARDIAN_PUSHER_NAME,
        "GUARDIAN_CLUSTER": GUARDIAN_CLUSTER,
        "GUARDIAN_PUSHERS": GUARDIAN_PUSHERS,
        "GUARDIAN_UI_LISTEN": GUARDIAN_UI_LISTEN,
        "GUARDIAN_UI_BASE_URL": GUARDIAN_UI_BASE_URL,
        "MONOFS_TOKEN": _b64(monofs_token),
        "CLIENT_DISCOVERY_TOKEN": _b64(cdt),
    }


def _render(name: str, extra: dict | None = None) -> str:
    v = _vars()
    if extra:
        v.update(extra)
    text = (TEMPLATES / "guardian" / name).read_text()
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
    from stratatools.image import cmd_build
    cmd_build(["guardian-configs"], dry_run=dry_run)


_DEPLOYS = ["guardiand", "guardian-pusher-k8s"]
_CLUSTER_ROLE_BINDINGS = [
    "guardian-cluster-admin",
    "guardian-pusher-cluster-admin",
]


def deploy(dry_run: bool) -> None:
    info(f"=== deploying guardian to namespace {NAMESPACE} ===")
    _apply(_render("namespace.yaml"), dry_run)
    _apply(_render("secret.yaml"), dry_run)
    _apply(_render("rbac.yaml"), dry_run)
    _apply(_render("svc-guardian-ui.yaml"), dry_run)
    _apply(_render("svc-guardian-ui-external.yaml"), dry_run)
    _apply(_render("deploy-guardiand.yaml"), dry_run)
    _apply(_render("deploy-pusher-k8s.yaml"), dry_run)
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


def stop(dry_run: bool) -> None:
    run(
        ["kubectl", "-n", NAMESPACE, "scale", "deployment", "--all", "--replicas=0"],
        check=False,
        dry_run=dry_run,
    )


def _clear_service_finalizers(dry_run: bool) -> None:
    if dry_run:
        info(f"+ kubectl -n {NAMESPACE} get service -o name")
        info(
            f"+ kubectl -n {NAMESPACE} patch service <name> --type=merge -p "
            "'{\"metadata\":{\"finalizers\":[]}}'"
        )
        return

    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "service", "-o", "name"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return

    for service in (line.strip() for line in result.stdout.splitlines()):
        if not service:
            continue
        run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "patch",
                service,
                "--type=merge",
                "-p",
                '{"metadata":{"finalizers":[]}}',
            ],
            check=False,
            dry_run=dry_run,
        )


def destroy(dry_run: bool) -> None:
    _clear_service_finalizers(dry_run)
    for name in _CLUSTER_ROLE_BINDINGS:
        run(
            [
                "kubectl",
                "delete",
                "clusterrolebinding",
                name,
                "--ignore-not-found",
            ],
            check=False,
            dry_run=dry_run,
        )
    run(
        [
            "kubectl",
            "delete",
            "namespace",
            NAMESPACE,
            "--ignore-not-found",
            "--wait=false",
        ],
        check=False,
        dry_run=dry_run,
    )


def _svc_ip(ns: str, name: str) -> str:
    r = subprocess.run(
        [
            "kubectl",
            "-n",
            ns,
            "get",
            "svc",
            name,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip}",
        ],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _resolve(ns: str, name: str, retries: int = 30, sleep_s: float = 5.0) -> str:
    for _ in range(retries):
        ip = _svc_ip(ns, name)
        if ip:
            return ip
        time.sleep(sleep_s)
    return ""


def _set_env_in_intent(path, env_name: str, value: str) -> bool:
    if not path.exists():
        return False
    docs = list(yaml.safe_load_all(path.read_text()))
    changed = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for env in _walk_envs(doc):
            if env.get("name") == env_name:
                env["value"] = value
                changed = True
    if changed:
        path.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    return changed


def _walk_envs(node):
    if isinstance(node, dict):
        if "env" in node and isinstance(node["env"], list):
            for e in node["env"]:
                if isinstance(e, dict):
                    yield e
        for v in node.values():
            yield from _walk_envs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_envs(v)


def _set_top_key(path, key: str, value: str) -> None:
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text()) or {}
    data[key] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def stamp_urls(dry_run: bool) -> None:
    gcp = PARTITIONS / "guardian-configs" / "intents" / "guardian-control-plane.yaml"
    gcfg = PARTITIONS / "guardian-configs" / "config.yaml"
    dq = PARTITIONS / "doctor" / "intents" / "query.yaml"
    dcfg = PARTITIONS / "doctor" / "config.yaml"

    if dry_run:
        info(f"would resolve guardian-ui-external in {NAMESPACE}")
        info(f"would update: {gcp}, {gcfg}, {dq}, {dcfg}")
        return

    ip = _resolve(NAMESPACE, "guardian-ui-external")
    if not ip:
        warn("guardian-ui-external has no LoadBalancer IP yet; skipping")
        return
    url = f"http://{ip}:8080"
    info(f"guardian UI URL: {url}")
    _set_env_in_intent(gcp, "GUARDIAN_UI_BASE_URL", url)
    _set_top_key(gcfg, "guardian_ui_base_url", url)
    _set_env_in_intent(dq, "GUARDIAN_UI_BASE_URL", url)

    dip = _svc_ip(NAMESPACE, "doctor-query-external")
    if dip:
        durl = f"http://{dip}:8080"
        info(f"doctor query URL: {durl}")
        _set_top_key(dcfg, "doctor_query_base_url", durl)
