"""Phase 1: MonoFS + MinIO storage bootstrap."""
from __future__ import annotations
import base64
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from string import Template

from stratatools.util import die, info, run, TEMPLATES, ROOT, warn

NAMESPACE = os.environ.get("MONOFS_NAMESPACE", "monofs")
EXTERNAL_SERVICE_TYPE = os.environ.get("EXTERNAL_SERVICE_TYPE", "LoadBalancer")
EXTERNAL_ADDRESS_TEMPLATE = os.environ.get("EXTERNAL_ADDRESS_TEMPLATE", "")
MINIO_PVC_SIZE = os.environ.get("MINIO_PVC_SIZE", "50Gi")
FETCHER_PVC_SIZE = os.environ.get("FETCHER_PVC_SIZE", "20Gi")
SEARCH_PVC_SIZE = os.environ.get("SEARCH_PVC_SIZE", "40Gi")
NODE_PVC_SIZE = os.environ.get("NODE_PVC_SIZE", "100Gi")
ROLLOUT_STATUS_TIMEOUT = os.environ.get("ROLLOUT_STATUS_TIMEOUT", "120s")
FORCE_DELETE_TIMEOUT = os.environ.get("FORCE_DELETE_TIMEOUT", "30s")
MONOFS_CLUSTER_ID = os.environ.get("MONOFS_CLUSTER_ID", "monofs-cluster")
MONOFS_SERVER_IMAGE = os.environ.get("MONOFS_SERVER_IMAGE", "monofs-server:latest")
MONOFS_ROUTER_IMAGE = os.environ.get("MONOFS_ROUTER_IMAGE", "monofs-router:latest")
MONOFS_FETCHER_IMAGE = os.environ.get("MONOFS_FETCHER_IMAGE", "monofs-fetcher:latest")
MONOFS_SEARCH_IMAGE = os.environ.get("MONOFS_SEARCH_IMAGE", "monofs-search:latest")
MINIO_IMAGE = os.environ.get("MINIO_IMAGE", "mirror.gcr.io/minio/minio:latest")
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", str(ROOT.parent / "monofs")))
NODE_NAMES = ("node-a", "node-b", "node-c", "node-d", "node-e")
ROUTER_SUFFIXES = ("a", "b")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _node_name(suffix: str) -> str:
    return suffix if suffix.startswith("node-") else f"node-{suffix}"


def _node_external_port(name: str) -> str:
    node_name = _node_name(name)
    return {
        "node-a": "9006",
        "node-b": "9002",
        "node-c": "9003",
        "node-d": "9004",
        "node-e": "9005",
    }.get(node_name, "9000")


def _default_external_addr(name: str) -> str:
    port = _node_external_port(name)
    if EXTERNAL_ADDRESS_TEMPLATE:
        return (
            EXTERNAL_ADDRESS_TEMPLATE.replace("{name}", name)
            .replace("{namespace}", NAMESPACE)
            .replace("{port}", port)
        )
    return f"{name}-external.{NAMESPACE}.svc.cluster.local:{port}"


def _default_external_addr_csv() -> str:
    return ",".join(f"{name}={_default_external_addr(name)}" for name in NODE_NAMES)


def _internal_node_addr_csv() -> str:
    return ",".join(f"{name}={name}:9000" for name in NODE_NAMES)


def _router_name(suffix: str) -> str:
    return f"router-{suffix}"


def _router_peer_name(suffix: str) -> str:
    return _router_name("b" if suffix == "a" else "a")


def _node_vars(suffix: str) -> dict[str, str]:
    return {
        "SUFFIX": suffix,
        "NODE_NAME": _node_name(suffix),
        "NODE_EXTERNAL_PORT": _node_external_port(suffix),
    }


def _router_vars(suffix: str, external_addr_csv: str | None = None) -> dict[str, str]:
    return {
        "SUFFIX": suffix,
        "ROUTER_NAME": _router_name(suffix),
        "ROUTER_PEER_NAME": _router_peer_name(suffix),
        "MONOFS_CLUSTER_ID": MONOFS_CLUSTER_ID,
        "MONOFS_NODE_ADDRS": _internal_node_addr_csv(),
        "MONOFS_EXTERNAL_ADDRS": external_addr_csv or _default_external_addr_csv(),
    }


def _kubectl_query(args: list[str]) -> str:
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_docker_internal_ip(host: str) -> bool:
    return bool(re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.", host)) or host.startswith(
        "192.168."
    )


def _service_type(service_name: str) -> str:
    return _kubectl_query(
        ["-n", NAMESPACE, "get", "service", service_name, "-o", "jsonpath={.spec.type}"]
    )


def _service_lb_host(service_name: str) -> str:
    host = _kubectl_query(
        [
            "-n",
            NAMESPACE,
            "get",
            "service",
            service_name,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ]
    )
    if not host:
        host = _kubectl_query(
            [
                "-n",
                NAMESPACE,
                "get",
                "service",
                service_name,
                "-o",
                "jsonpath={.status.loadBalancer.ingress[0].ip}",
            ]
        )
    if _is_docker_internal_ip(host):
        return "127.0.0.1"
    return host


def _service_node_port(service_name: str) -> str:
    return _kubectl_query(
        [
            "-n",
            NAMESPACE,
            "get",
            "service",
            service_name,
            "-o",
            "jsonpath={.spec.ports[0].nodePort}",
        ]
    )


def _first_node_address() -> str:
    address = _kubectl_query(
        [
            "get",
            "nodes",
            "-o",
            "jsonpath={.items[0].status.addresses[?(@.type=='ExternalIP')].address}",
        ]
    )
    if address:
        return address
    return _kubectl_query(
        [
            "get",
            "nodes",
            "-o",
            "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
        ]
    )


def _service_external_endpoint(service_name: str, service_port: str) -> str:
    service_type = _service_type(service_name)
    if service_type == "LoadBalancer":
        host = _service_lb_host(service_name)
        return f"{host}:{service_port}" if host else ""
    if service_type == "NodePort":
        host = _first_node_address()
        node_port = _service_node_port(service_name)
        return f"{host}:{node_port}" if host and node_port else ""
    return ""


def _discover_external_addr_csv() -> str:
    external_addrs: list[str] = []
    for node_name in NODE_NAMES:
        endpoint = _service_external_endpoint(
            f"{node_name}-external", _node_external_port(node_name)
        )
        if not endpoint:
            return ""
        external_addrs.append(f"{node_name}={endpoint}")
    return ",".join(external_addrs)


def _deployment_selector(deployment: str) -> str:
    raw = _kubectl_query(["-n", NAMESPACE, "get", "deployment", deployment, "-o", "json"])
    if not raw:
        return ""
    try:
        match_labels = json.loads(raw)["spec"]["selector"]["matchLabels"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return ""
    return ",".join(f"{key}={value}" for key, value in match_labels.items())


def _terminating_pods_for_deployment(deployment: str) -> list[str]:
    selector = _deployment_selector(deployment)
    if not selector:
        return []

    raw = _kubectl_query(["-n", NAMESPACE, "get", "pods", "-l", selector, "-o", "json"])
    if not raw:
        return []
    try:
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        return []
    return [
        item["metadata"]["name"]
        for item in items
        if item.get("metadata", {}).get("deletionTimestamp")
    ]


def _wait_rollout(deployment: str, dry_run: bool) -> None:
    cmd = [
        "kubectl",
        "-n",
        NAMESPACE,
        "rollout",
        "status",
        f"deployment/{deployment}",
        f"--timeout={ROLLOUT_STATUS_TIMEOUT}",
    ]
    result = run(cmd, check=False, dry_run=dry_run)
    if dry_run or (result and result.returncode == 0):
        return

    terminating_pods = _terminating_pods_for_deployment(deployment)
    if not terminating_pods:
        die(
            f"deployment/{deployment} did not finish rolling out within "
            f"{ROLLOUT_STATUS_TIMEOUT}"
        )

    warn(
        f"force deleting stuck terminating pods for deployment/{deployment}: "
        f"{', '.join(terminating_pods)}"
    )
    for pod in terminating_pods:
        run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "delete",
                "pod",
                pod,
                "--force",
                "--grace-period=0",
            ],
            check=False,
            dry_run=dry_run,
        )
    run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "wait",
            "--for=delete",
            "pod",
            *terminating_pods,
            f"--timeout={FORCE_DELETE_TIMEOUT}",
        ],
        check=False,
        dry_run=dry_run,
    )

    result = run(cmd, check=False, dry_run=dry_run)
    if not dry_run and result and result.returncode != 0:
        die(
            f"deployment/{deployment} did not finish rolling out within "
            f"{ROLLOUT_STATUS_TIMEOUT}"
        )


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
        "MONOFS_CLUSTER_ID": MONOFS_CLUSTER_ID,
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
        "MONOFS_NODE_ADDRS": _internal_node_addr_csv(),
        "MONOFS_EXTERNAL_ADDRS": _default_external_addr_csv(),
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


def _apply_manifests(dry_run: bool) -> None:
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
        _apply(_render("deploy-node.yaml", _node_vars(s)), dry_run)
    for s in ROUTER_SUFFIXES:
        _apply(_render("deploy-router.yaml", _router_vars(s)), dry_run)
    _apply(_render("deploy-haproxy.yaml"), dry_run)
    _apply(_render("svc-minio.yaml"), dry_run)
    for s in ("a", "b"):
        _apply(_render("svc-fetcher.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("svc-search-index.yaml"), dry_run)
    for s in ("a", "b", "c", "d", "e"):
        _apply(_render("svc-node.yaml", _node_vars(s)), dry_run)
    for s in ("a", "b"):
        _apply(_render("svc-router.yaml", {"SUFFIX": s}), dry_run)
    _apply(_render("svc-haproxy.yaml"), dry_run)


def deploy(dry_run: bool) -> None:
    info(f"=== deploying storage to namespace {NAMESPACE} ===")
    _apply_manifests(dry_run)
    _wait_rollouts(dry_run)
    _reconfigure_router_external_addresses(dry_run)


def _wait_rollouts(dry_run: bool, deployments: list[str] | None = None) -> None:
    for d in deployments or _DEPLOYS:
        _wait_rollout(d, dry_run)


def _reconfigure_router_external_addresses(dry_run: bool) -> None:
    if EXTERNAL_ADDRESS_TEMPLATE:
        return
    if dry_run:
        info("skipping router external address discovery during dry run")
        return

    external_addr_csv = _discover_external_addr_csv()
    if not external_addr_csv:
        warn(
            "could not resolve host-reachable MonoFS external node endpoints yet; "
            "guardianctl outside Kubernetes may still need EXTERNAL_ADDRESS_TEMPLATE "
            "or a later deploy/rollout"
        )
        return

    info(f"configured MonoFS router external node addresses: {external_addr_csv}")
    for s in ROUTER_SUFFIXES:
        _apply(_render("deploy-router.yaml", _router_vars(s, external_addr_csv)), dry_run)
    _wait_rollouts(dry_run, deployments=[_router_name(s) for s in ROUTER_SUFFIXES])


def rollout(dry_run: bool) -> None:
    _apply_manifests(dry_run)
    run(
        ["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment"],
        check=False,
        dry_run=dry_run,
    )
    _wait_rollouts(dry_run)
    _reconfigure_router_external_addresses(dry_run)


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
