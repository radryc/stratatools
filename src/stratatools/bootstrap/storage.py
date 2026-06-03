"""Phase 1: MonoFS + MinIO storage bootstrap."""
from __future__ import annotations
import base64
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from string import Template

from stratatools.monofs_key import is_valid_monofs_encryption_key, resolve_monofs_encryption_key_state
from stratatools.util import die, info, run, TEMPLATES, ROOT, warn

NAMESPACE = os.environ.get("MONOFS_NAMESPACE", "monofs")
LB_NAMESPACE = os.environ.get("LB_NAMESPACE", "lb-edge")
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
MONOFS_LB_IMAGE = os.environ.get("MONOFS_LB_IMAGE", "lb:latest")
MINIO_IMAGE = os.environ.get("MINIO_IMAGE", "mirror.gcr.io/minio/minio:latest")
MONOFS_IMAGE_PULL_POLICY = os.environ.get("MONOFS_IMAGE_PULL_POLICY", "IfNotPresent")
MONOFS_OTEL_ENDPOINT = os.environ.get("MONOFS_OTEL_ENDPOINT", "")
MONOFS_OTEL_INSECURE = os.environ.get("MONOFS_OTEL_INSECURE", "true")
MONOFS_OTEL_SERVICE_NAME = os.environ.get("MONOFS_OTEL_SERVICE_NAME", "monofs-server")
MONOFS_OTEL_METRIC_INTERVAL = os.environ.get("MONOFS_OTEL_METRIC_INTERVAL", "30s")
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", str(ROOT.parent / "monofs")))
LB_REPO_DIR = Path(os.environ.get("LB_REPO_DIR", str(ROOT.parent / "lb")))
PORT_FORWARD_STATE_DIR = Path.home() / ".stratatools" / "port-forwards"
MONOFS_PORT_FORWARD_PID_FILE = PORT_FORWARD_STATE_DIR / "monofs-external.pid"
MONOFS_PORT_FORWARD_LOG_FILE = PORT_FORWARD_STATE_DIR / "monofs-external.log"
MONOFS_PORT_FORWARD_CMD_FILE = PORT_FORWARD_STATE_DIR / "monofs-external.cmd"
MONOFS_LOCAL_HTTP_PORT = int(os.environ.get("MONOFS_LOCAL_HTTP_PORT", "8080"))
MONOFS_LOCAL_GRPC_PORT = int(os.environ.get("MONOFS_LOCAL_GRPC_PORT", "9090"))
GUARDIAN_LOCAL_UI_PORT = int(os.environ.get("GUARDIAN_UI_PORT", "8090"))
LB_LOCAL_ADMIN_PORT = int(os.environ.get("LB_LOCAL_ADMIN_PORT", "18081"))
NODE_NAMES = ("node-a", "node-b", "node-c", "node-d", "node-e")
ROUTER_SUFFIXES = ("a", "b")

# Extra ports for user-facing services registered dynamically by lb-k8s-agent
# (e.g. agent frontend=9191, vscode=8888). These are added to both the
# monofs-external K8s Service and the local port-forward so they're reachable
# on 172.21.63.46:<port> from Windows. Space-separated list of port numbers.
_LB_EXTRA_PORTS_RAW = os.environ.get("LB_USER_SERVICE_PORTS", "9191 8888")
LB_USER_SERVICE_PORTS: list[int] = [
    int(p) for p in _LB_EXTRA_PORTS_RAW.split() if p.strip().isdigit()
]


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


def _lb_node_bootstrap(namespace: str) -> str:
    """LB_BOOTSTRAP entries routing each node's external port → internal ClusterIP gRPC.

    External tools (e.g. guardianctl) dial individual node addresses that the MonoFS
    router advertises when useExternalAddresses=true.  Those addresses resolve to
    172.21.63.46:9002-9006 which lb-edge listens on and forwards in-cluster.
    """
    entries = []
    for name in NODE_NAMES:
        port = _node_external_port(name)
        internal = f"{name}.{namespace}.svc.cluster.local:9000"
        entries.append(f"{name}@grpc[MonoFS {name} gRPC]:{port}={internal}")
    return ";".join(entries)


def _lb_node_ports_yaml() -> str:
    """YAML port entries added to the monofs-external Service for each node port."""
    lines = []
    for name in NODE_NAMES:
        port = _node_external_port(name)
        lines.append(
            f"    - name: {name}-grpc\n"
            f"      port: {port}\n"
            f"      targetPort: {port}"
        )
    return "\n".join(lines)


def _lb_node_container_ports_yaml() -> str:
    """containerPort entries for the lb-edge Deployment for each node port."""
    lines = []
    for name in NODE_NAMES:
        port = _node_external_port(name)
        lines.append(f"            - containerPort: {port}")
    return "\n".join(lines)


def _lb_user_service_ports_yaml() -> str:
    """YAML port entries for user-facing services registered via lb-k8s-agent."""
    lines = []
    for port in LB_USER_SERVICE_PORTS:
        lines.append(
            f"    - name: user-svc-{port}\n"
            f"      port: {port}\n"
            f"      targetPort: {port}"
        )
    return "\n".join(lines)


def _lb_user_service_container_ports_yaml() -> str:
    """containerPort entries for the lb-edge Deployment for user service ports."""
    return "\n".join(f"            - containerPort: {p}" for p in LB_USER_SERVICE_PORTS)


def _internal_node_addr(name: str) -> str:
    return f"{name}.{NAMESPACE}.svc.cluster.local:9000"


def _internal_node_addr_csv() -> str:
    return ",".join(f"{name}={_internal_node_addr(name)}" for name in NODE_NAMES)


def _router_name(suffix: str) -> str:
    return f"router-{suffix}"


def _router_peer_name(suffix: str) -> str:
    return _router_name("b" if suffix == "a" else "a")


def _kvs_extra_args(suffix: str) -> str:
    """Build KVS bootstrap/peer args for a node, indented for YAML args list."""
    node_name = _node_name(suffix)
    lines: list[str] = []
    all_nodes = NODE_NAMES  # ("node-a", ..., "node-e")
    if node_name == "node-a":
        lines.append("            - --kvs-bootstrap")
    for peer in all_nodes:
        if peer != node_name:
            lines.append(f"            - --kvs-peer={peer},{peer}:9000,{peer}:7000")
    return "\n".join(lines) + "\n" if lines else ""


def _node_vars(suffix: str) -> dict[str, str]:
    return {
        "SUFFIX": suffix,
        "NODE_NAME": _node_name(suffix),
        "NODE_EXTERNAL_PORT": _node_external_port(suffix),
        "KVS_EXTRA_ARGS": _kvs_extra_args(suffix),
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


def _configured_external_service_ips() -> list[str]:
    raw = (
        os.environ.get("EXTERNAL_SERVICE_IPS", "").strip()
        or os.environ.get("EXTERNAL_SERVICE_IP", "").strip()
    )
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _external_service_spec_yaml(indent: int = 2) -> str:
    ips = _configured_external_service_ips()
    if not ips:
        return ""
    prefix = " " * indent
    nested = " " * (indent + 2)
    lines = [f"{prefix}externalIPs:"]
    lines.extend(f"{nested}- {ip}" for ip in ips)
    return "\n" + "\n".join(lines)


def _service_type(service_name: str) -> str:
    return _kubectl_query(
        ["-n", NAMESPACE, "get", "service", service_name, "-o", "jsonpath={.spec.type}"]
    )


def _service_explicit_host(service_name: str) -> str:
    host = _kubectl_query(
        [
            "-n",
            NAMESPACE,
            "get",
            "service",
            service_name,
            "-o",
            "jsonpath={.spec.externalIPs[0]}",
        ]
    )
    if host:
        return host
    return _kubectl_query(
        [
            "-n",
            NAMESPACE,
            "get",
            "service",
            service_name,
            "-o",
            "jsonpath={.spec.loadBalancerIP}",
        ]
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


def _host_reachable_node_address() -> str:
    override = os.environ.get("MONOFS_NODE_HOST", "").strip()
    if override:
        return override
    host = _first_node_address()
    if _is_docker_internal_ip(host):
        # On WSL2 the Docker bridge network is directly reachable from the host;
        # on Mac/Windows Docker Desktop it is not (use 127.0.0.1 + NodePort).
        # Probe the kube API server to decide.
        try:
            with socket.create_connection((host, 6443), timeout=1):
                return host
        except OSError:
            pass
        return "127.0.0.1"
    return host


def _service_external_endpoint(service_name: str, service_port: str) -> str:
    host = _service_explicit_host(service_name)
    if host:
        return f"{host}:{service_port}"
    service_type = _service_type(service_name)
    if service_type == "LoadBalancer":
        host = _service_lb_host(service_name)
        if host:
            return f"{host}:{service_port}"
        host = _host_reachable_node_address()
        node_port = _service_node_port(service_name)
        return f"{host}:{node_port}" if host and node_port else ""
    if service_type == "NodePort":
        host = _host_reachable_node_address()
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


def _deployment_selector(deployment: str, namespace: str = NAMESPACE) -> str:
    raw = _kubectl_query(["-n", namespace, "get", "deployment", deployment, "-o", "json"])
    if not raw:
        return ""
    try:
        match_labels = json.loads(raw)["spec"]["selector"]["matchLabels"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return ""
    return ",".join(f"{key}={value}" for key, value in match_labels.items())


def _terminating_pods_for_deployment(deployment: str, namespace: str = NAMESPACE) -> list[str]:
    selector = _deployment_selector(deployment, namespace)
    if not selector:
        return []

    raw = _kubectl_query(["-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
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


def _wait_rollout(deployment: str, dry_run: bool, namespace: str = NAMESPACE) -> None:
    cmd = [
        "kubectl",
        "-n",
        namespace,
        "rollout",
        "status",
        f"deployment/{deployment}",
        f"--timeout={ROLLOUT_STATUS_TIMEOUT}",
    ]
    result = run(cmd, check=False, dry_run=dry_run)
    if dry_run or (result and result.returncode == 0):
        return

    terminating_pods = _terminating_pods_for_deployment(deployment, namespace)
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
                namespace,
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
            namespace,
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
    monofs_encryption_key = resolve_monofs_encryption_key_state(
        repo_dir=MONOFS_REPO_DIR
    ).key
    if not is_valid_monofs_encryption_key(monofs_encryption_key):
        die(
            "MONOFS_ENCRYPTION_KEY is not configured. Run `st-setup` first or set "
            "MONOFS_ENCRYPTION_KEY before bootstrap."
        )
    minio_ak = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
    minio_sk = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
    return {
        "NAMESPACE": NAMESPACE,
        "EXTERNAL_SERVICE_TYPE": EXTERNAL_SERVICE_TYPE,
        "EXTERNAL_SERVICE_SPEC": _external_service_spec_yaml(),
        "MONOFS_CLUSTER_ID": MONOFS_CLUSTER_ID,
        "MINIO_PVC_SIZE": MINIO_PVC_SIZE,
        "FETCHER_PVC_SIZE": FETCHER_PVC_SIZE,
        "SEARCH_PVC_SIZE": SEARCH_PVC_SIZE,
        "NODE_PVC_SIZE": NODE_PVC_SIZE,
        "MONOFS_SERVER_IMAGE": MONOFS_SERVER_IMAGE,
        "MONOFS_ROUTER_IMAGE": MONOFS_ROUTER_IMAGE,
        "MONOFS_FETCHER_IMAGE": MONOFS_FETCHER_IMAGE,
        "MONOFS_SEARCH_IMAGE": MONOFS_SEARCH_IMAGE,
        "MONOFS_LB_IMAGE": MONOFS_LB_IMAGE,
        "MINIO_IMAGE": MINIO_IMAGE,
        "MONOFS_IMAGE_PULL_POLICY": MONOFS_IMAGE_PULL_POLICY,
        "MONOFS_OTEL_ENDPOINT": MONOFS_OTEL_ENDPOINT,
        "MONOFS_OTEL_INSECURE": MONOFS_OTEL_INSECURE,
        "MONOFS_OTEL_SERVICE_NAME": MONOFS_OTEL_SERVICE_NAME,
        "MONOFS_OTEL_METRIC_INTERVAL": MONOFS_OTEL_METRIC_INTERVAL,
        "MONOFS_TOKEN": _b64(monofs_token),
        "MONOFS_ENCRYPTION_KEY": _b64(monofs_encryption_key),
        "MINIO_ACCESS_KEY": _b64(minio_ak),
        "MINIO_SECRET_KEY": _b64(minio_sk),
        "MONOFS_NODE_ADDRS": _internal_node_addr_csv(),
        "MONOFS_EXTERNAL_ADDRS": _default_external_addr_csv(),
        "SUFFIX": "",
        # guardian vars needed by deploy-haproxy.yaml (lb-edge proxies guardian UI)
        "GUARDIAN_NAMESPACE": os.environ.get("GUARDIAN_NAMESPACE", "guardian"),
        "GUARDIAN_UI_PORT": os.environ.get("GUARDIAN_UI_PORT", "8090"),
        # lb-edge dedicated namespace
        "LB_NAMESPACE": LB_NAMESPACE,
        # lb-edge node bootstrap: node-X frontend ports forwarded to cluster-internal gRPC.
        # External tools (guardianctl) dial 172.21.63.46:9002-9006; lb-edge proxies them
        # to node-X.monofs.svc.cluster.local:9000.  Internal services use ClusterIP directly.
        "LB_NODE_BOOTSTRAP": _lb_node_bootstrap(NAMESPACE),
        "LB_NODE_PORTS_SPEC": _lb_node_ports_yaml(),
        "LB_NODE_CONTAINER_PORTS": _lb_node_container_ports_yaml(),
        # User-facing service ports registered dynamically by lb-k8s-agent
        # (agent frontend, vscode, etc.). Must be in K8s Service and port-forward.
        "LB_USER_SERVICE_PORTS_SPEC": _lb_user_service_ports_yaml(),
        "LB_USER_SERVICE_CONTAINER_PORTS": _lb_user_service_container_ports_yaml(),
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
    run(
        ["docker", "build", "-t", MONOFS_LB_IMAGE, "-f", str(LB_REPO_DIR / "Dockerfile"), str(LB_REPO_DIR)],
        check=True,
        dry_run=dry_run,
    )


def load_images(dry_run: bool) -> None:
    from stratatools.image import _cluster_load, _cluster_load_mode, kubectl_context

    ctx = kubectl_context()
    if not _cluster_load_mode(ctx):
        return

    info(f"=== loading storage images into cluster context {ctx} ===")
    for image in [
        MONOFS_SERVER_IMAGE,
        MONOFS_ROUTER_IMAGE,
        MONOFS_FETCHER_IMAGE,
        MONOFS_SEARCH_IMAGE,
        MONOFS_LB_IMAGE,
    ]:
        _cluster_load(image, dry_run)


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME", "").strip():
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _lb_edge_registered_ports() -> list[int]:
    """Query lb-edge registry and return all registered external ports.

    Used to make the port-forward cover every port lb-edge is serving.
    Returns empty list if lb-edge isn't reachable yet.
    """
    try:
        url = f"http://127.0.0.1:{LB_LOCAL_ADMIN_PORT}/services"
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read())
        return sorted({int(s["external_port"]) for s in data.get("services", [])})
    except Exception:
        return []


def _local_port_forward_address() -> str:
    configured = os.environ.get("MONOFS_PORT_FORWARD_ADDRESS", "").strip()
    if configured:
        return configured
    return "0.0.0.0"


def _legacy_local_port_forward_command() -> list[str]:
    return [
        "kubectl",
        "-n",
        LB_NAMESPACE,
        "port-forward",
        "svc/monofs-external",
        f"{MONOFS_LOCAL_HTTP_PORT}:8080",
        f"{MONOFS_LOCAL_GRPC_PORT}:9090",
        f"{GUARDIAN_LOCAL_UI_PORT}:8090",
        f"{LB_LOCAL_ADMIN_PORT}:18081",
    ]


def _local_port_forward_command() -> list[str]:
    command = [
        "kubectl",
        "-n",
        LB_NAMESPACE,
        "port-forward",
    ]
    address = _local_port_forward_address()
    if address:
        command.extend(["--address", address])

    # Static ports always forwarded.
    static_ports = (
        [
            f"{MONOFS_LOCAL_HTTP_PORT}:8080",
            f"{MONOFS_LOCAL_GRPC_PORT}:9090",
            f"{GUARDIAN_LOCAL_UI_PORT}:8090",
            f"{LB_LOCAL_ADMIN_PORT}:18081",
        ]
        + [f"{_node_external_port(n)}:{_node_external_port(n)}" for n in NODE_NAMES]
        + [f"{p}:{p}" for p in LB_USER_SERVICE_PORTS]
    )
    static_port_nums = {
        8080, 9090, 8090, 18081,
        *{int(_node_external_port(n)) for n in NODE_NAMES},
        *LB_USER_SERVICE_PORTS,
    }

    # Dynamic: any ports registered in lb-edge that aren't already covered.
    dynamic_ports = [
        f"{p}:{p}"
        for p in _lb_edge_registered_ports()
        if p not in static_port_nums
    ]

    command.extend(["svc/monofs-external"] + static_ports + dynamic_ports)
    return command


def _local_port_open(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.5)
            sock.connect((host, port))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def _local_ports_open() -> bool:
    return (
        _local_port_open(MONOFS_LOCAL_HTTP_PORT)
        and _local_port_open(MONOFS_LOCAL_GRPC_PORT)
        and _local_port_open(GUARDIAN_LOCAL_UI_PORT)
        and _local_port_open(LB_LOCAL_ADMIN_PORT)
    )


def _managed_port_forward_pid() -> int | None:
    try:
        raw = MONOFS_PORT_FORWARD_PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _managed_port_forward_command() -> list[str] | None:
    try:
        raw = MONOFS_PORT_FORWARD_CMD_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return None
    return command


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _clear_managed_port_forward_state() -> None:
    MONOFS_PORT_FORWARD_PID_FILE.unlink(missing_ok=True)
    MONOFS_PORT_FORWARD_CMD_FILE.unlink(missing_ok=True)


def ensure_local_port_forward(dry_run: bool) -> None:
    command = _local_port_forward_command()
    if dry_run:
        info("+ " + " ".join(command))
        return

    pid = _managed_port_forward_pid()
    if pid and _pid_alive(pid):
        managed_command = _managed_port_forward_command()
        if managed_command is None:
            managed_command = _legacy_local_port_forward_command()
        if managed_command != command:
            info("restarting monofs localhost port-forward to apply updated bind settings")
            stop_local_port_forward(False)
            pid = None
        elif _local_ports_open():
            info(
                f"monofs localhost ports already exposed on {MONOFS_LOCAL_HTTP_PORT} and {MONOFS_LOCAL_GRPC_PORT}"
            )
            return

    if pid and not _pid_alive(pid):
        _clear_managed_port_forward_state()

    if _local_ports_open():
        info(
            f"monofs localhost ports already in use on {MONOFS_LOCAL_HTTP_PORT} and {MONOFS_LOCAL_GRPC_PORT}; leaving existing forwarder in place"
        )
        return

    PORT_FORWARD_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with MONOFS_PORT_FORWARD_LOG_FILE.open("a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    MONOFS_PORT_FORWARD_PID_FILE.write_text(f"{proc.pid}\n", encoding="utf-8")
    MONOFS_PORT_FORWARD_CMD_FILE.write_text(json.dumps(command), encoding="utf-8")

    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if _local_ports_open():
            info(
                f"exposed monofs on localhost:{MONOFS_LOCAL_HTTP_PORT} and localhost:{MONOFS_LOCAL_GRPC_PORT}"
            )
            return
        time.sleep(0.2)

    detail = ""
    try:
        detail = MONOFS_PORT_FORWARD_LOG_FILE.read_text(encoding="utf-8")[-400:].strip()
    except OSError:
        pass
    _clear_managed_port_forward_state()
    die(
        "failed to expose monofs localhost ports via kubectl port-forward"
        + (f": {detail}" if detail else "")
    )


def stop_local_port_forward(dry_run: bool) -> None:
    pid = _managed_port_forward_pid()
    if pid is None:
        return
    info(
        f"stopping port-forward on {MONOFS_LOCAL_HTTP_PORT}, {MONOFS_LOCAL_GRPC_PORT} and {GUARDIAN_LOCAL_UI_PORT}"
    )
    if dry_run:
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        _clear_managed_port_forward_state()
        return

    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            _clear_managed_port_forward_state()
            return
        time.sleep(0.1)

    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    _clear_managed_port_forward_state()


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
]


def _apply_manifests(dry_run: bool) -> None:
    _apply(_render("namespace.yaml"), dry_run)
    _apply(_render("secret.yaml"), dry_run)
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
    _apply(_render("ns-lb-edge.yaml"), dry_run)
    _apply(_render("rbac-lb-agent.yaml"), dry_run)
    _apply(_render("configmap-lb-port-sync.yaml"), dry_run)
    _apply(_render("deploy-lb-k8s-agent.yaml"), dry_run)
    _apply(_render("deploy-lb-port-sync.yaml"), dry_run)
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


def _wait_lb_edge_rollout(dry_run: bool) -> None:
    _wait_rollout("monofs-haproxy", dry_run, LB_NAMESPACE)
    # lb-k8s-agent holds in-memory registry state. After haproxy restarts the
    # registry is empty, so restart the agent to trigger a full endpoint re-sync.
    run(
        ["kubectl", "-n", LB_NAMESPACE, "rollout", "restart", "deployment/lb-k8s-agent"],
        check=False,
        dry_run=dry_run,
    )
    _wait_rollout("lb-k8s-agent", dry_run, LB_NAMESPACE)


def deploy(dry_run: bool) -> None:
    info(f"=== deploying storage to namespace {NAMESPACE} ===")
    _apply_manifests(dry_run)
    _wait_rollouts(dry_run)
    _wait_lb_edge_rollout(dry_run)
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
    run(
        ["kubectl", "-n", LB_NAMESPACE, "rollout", "restart", "deployment"],
        check=False,
        dry_run=dry_run,
    )
    _wait_rollouts(dry_run)
    _wait_lb_edge_rollout(dry_run)
    _reconfigure_router_external_addresses(dry_run)


def stop(dry_run: bool) -> None:
    stop_local_port_forward(dry_run)
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
    stop_local_port_forward(dry_run)
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
