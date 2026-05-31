"""Local devdns lifecycle and route sync for bootstrap-managed developer hostnames."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

import yaml

from stratatools.util import PARTITIONS, ROOT, info, kubectl_context, warn

AINFRA = ROOT.parent
LOCAL_BIN_DIR = Path(
    os.environ.get("STRATATOOLS_BIN_DIR", str(Path.home() / "bin"))
).expanduser()
DEVDNS_PROXY_ADDR_ENV = "DEVDNS_PROXY_ADDR"
DEVDNS_PROXY_ADDR_DEFAULT = "127.0.0.1:80"
DEVDNS_REPO_DIR = Path(os.environ.get("DEVDNS_REPO_DIR", str(AINFRA / "devdns"))).expanduser()
STATE_DIR = Path(
    os.environ.get("STRATATOOLS_DEVDNS_STATE_DIR", str(Path.home() / ".stratatools" / "devdns"))
).expanduser()
LOG_DIR = STATE_DIR / "logs"
ROUTE_STATE_DIR = STATE_DIR / "routes"
DAEMON_PID_FILE = STATE_DIR / "devdns.pid"
DAEMON_STATE_FILE = STATE_DIR / "daemon.json"
ROUTES_FILE = Path(
    os.environ.get("STRATATOOLS_DEVDNS_ROUTES_FILE", str(STATE_DIR / "routes.json"))
).expanduser()
DEVDNS_BIN = Path(os.environ.get("DEVDNS_BIN", str(LOCAL_BIN_DIR / "devdns"))).expanduser()
DEVDNSCTL_BIN = Path(os.environ.get("DEVDNSCTL_BIN", str(LOCAL_BIN_DIR / "devdnsctl"))).expanduser()
DEVDNS_DOMAIN = os.environ.get("DEVDNS_DOMAIN", "strata").strip() or "strata"
DEVDNS_SERVER_IP = os.environ.get("DEVDNS_SERVER_IP", "").strip()
DEVDNS_DNS_ADDR = os.environ.get("DEVDNS_DNS_ADDR", "127.0.0.1:5353")
DEVDNS_PROXY_ADDR = os.environ.get(DEVDNS_PROXY_ADDR_ENV, DEVDNS_PROXY_ADDR_DEFAULT).strip() or DEVDNS_PROXY_ADDR_DEFAULT
DEVDNS_ADMIN_ADDR = os.environ.get("DEVDNS_ADMIN_ADDR", "127.0.0.1:10090")
DEVDNS_PORT_START = int(os.environ.get("DEVDNS_PORT_START", "11080"))
DEVDNS_PORT_END = int(os.environ.get("DEVDNS_PORT_END", "11180"))
DEVDNS_ADMIN_URL = os.environ.get("DEVDNS_ADMIN_URL", f"http://{DEVDNS_ADMIN_ADDR}").rstrip("/")
DEVDNS_PROXY_FALLBACK_PORT = int(os.environ.get("DEVDNS_PROXY_FALLBACK_PORT", "18080"))


@dataclass(frozen=True)
class DeclaredRoute:
    partition: str
    intent: str
    asset: str
    hostname: str
    namespace: str
    resource: str
    remote_port: str

    @property
    def route_id(self) -> str:
        return "-".join(_sanitize_part(part) for part in (self.partition, self.intent, self.asset))


def local_bin_targets() -> list[tuple[str, Path, str]]:
    return [
        ("devdns", DEVDNS_REPO_DIR, "./cmd/devdns"),
        ("devdnsctl", DEVDNS_REPO_DIR, "./cmd/devdnsctl"),
    ]


def has_active_daemon() -> bool:
    try:
        with urlrequest.urlopen(f"{DEVDNS_ADMIN_URL}/healthz", timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError, urlerror.URLError):
        return False


def ensure_daemon(dry_run: bool) -> None:
    if has_active_daemon():
        info(f"devdns admin already available at {DEVDNS_ADMIN_URL}")
        return
    if not DEVDNS_BIN.exists():
        raise SystemExit(
            f"error: missing devdns binary at {DEVDNS_BIN}. Run st-bootstrap build|deploy|rollout --dns first."
        )

    _ensure_state_dirs(dry_run)
    _stop_pid_file(DAEMON_PID_FILE, dry_run=dry_run)
    log_path = LOG_DIR / "devdns.log"
    proxy_addrs = _startup_proxy_addrs()

    for index, proxy_addr in enumerate(proxy_addrs):
        command = _daemon_command(proxy_addr)
        info("+ " + " ".join(command))
        if dry_run:
            return

        if log_path.exists():
            log_path.unlink()
        with log_path.open("a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        DAEMON_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        deadline = time.time() + 10
        while time.time() < deadline:
            if has_active_daemon():
                _write_daemon_state(proc.pid, proxy_addr)
                info(f"devdns is listening at {proxy_addr} with admin {DEVDNS_ADMIN_URL}")
                return
            if proc.poll() is not None:
                tail = _tail(log_path)
                if index + 1 < len(proxy_addrs) and _is_bind_permission_denied(tail):
                    warn(
                        f"devdns could not bind {proxy_addr}; retrying with {proxy_addrs[index + 1]}"
                    )
                    break
                _clear_daemon_state()
                _stop_pid_file(DAEMON_PID_FILE, dry_run=False)
                raise SystemExit(
                    "error: devdns exited during startup. "
                    f"Check {log_path}. Last output:\n{tail}"
                )
            time.sleep(0.25)
        else:
            _clear_daemon_state()
            _stop_pid_file(DAEMON_PID_FILE, dry_run=False)
            raise SystemExit(
                f"error: devdns did not become healthy at {DEVDNS_ADMIN_URL}/healthz within 10s"
            )

    raise SystemExit("error: devdns could not be started")


def stop(dry_run: bool) -> None:
    for meta_path in sorted(ROUTE_STATE_DIR.glob("*.json")):
        _stop_route_meta(meta_path, dry_run=dry_run)
    _stop_pid_file(DAEMON_PID_FILE, dry_run=dry_run)
    if not dry_run:
        _clear_daemon_state()


def sync_routes(
    partitions: Iterable[str] | None,
    *,
    dry_run: bool,
    ensure_running: bool,
) -> None:
    routes = _declared_routes(partitions)
    if ensure_running:
        ensure_daemon(dry_run)
    elif not has_active_daemon() and not dry_run:
        return

    _ensure_state_dirs(dry_run)
    desired_ids: set[str] = set()
    for route in routes:
        if not _resource_exists(route.namespace, route.resource, dry_run=dry_run):
            warn(
                f"devdns route {route.hostname} is declared but {route.resource} is not ready in namespace {route.namespace}; skipping for now"
            )
            continue
        desired_ids.add(route.route_id)
        _ensure_route_process(route, dry_run=dry_run)

    for meta_path in sorted(ROUTE_STATE_DIR.glob("*.json")):
        if meta_path.stem not in desired_ids:
            _stop_route_meta(meta_path, dry_run=dry_run)


def first_route_url(partition: str) -> str | None:
    routes = _declared_routes([partition])
    if not routes:
        return None
    return route_url(routes[0].hostname)


def route_url(hostname: str) -> str:
    port = _address_port(_active_proxy_addr(), default=80)
    host = hostname.strip()
    if port == 80:
        return f"http://{host}"
    return f"http://{host}:{port}"


def _daemon_command(proxy_addr: str) -> list[str]:
    command = [
        str(DEVDNS_BIN),
        "--domain",
        DEVDNS_DOMAIN,
        "--dns-addr",
        DEVDNS_DNS_ADDR,
        "--proxy-addr",
        proxy_addr,
        "--admin-addr",
        DEVDNS_ADMIN_ADDR,
        "--routes-file",
        str(ROUTES_FILE),
        "--port-start",
        str(DEVDNS_PORT_START),
        "--port-end",
        str(DEVDNS_PORT_END),
    ]
    if DEVDNS_SERVER_IP:
        command.extend(["--server-ip", DEVDNS_SERVER_IP])
    return command


def _startup_proxy_addrs(proxy_addr: str | None = None, *, explicit: bool | None = None) -> list[str]:
    base_addr = (proxy_addr or DEVDNS_PROXY_ADDR).strip() or DEVDNS_PROXY_ADDR_DEFAULT
    explicitly_configured = _proxy_addr_is_explicit() if explicit is None else explicit
    proxy_addrs = [base_addr]
    if explicitly_configured:
        return proxy_addrs
    if _address_port(base_addr, default=80) != 80:
        return proxy_addrs
    fallback_addr = _replace_port(base_addr, DEVDNS_PROXY_FALLBACK_PORT)
    if fallback_addr != base_addr:
        proxy_addrs.append(fallback_addr)
    return proxy_addrs


def _proxy_addr_is_explicit() -> bool:
    value = os.environ.get(DEVDNS_PROXY_ADDR_ENV)
    return value is not None and value.strip() != ""


def _active_proxy_addr() -> str:
    state = _load_json(DAEMON_STATE_FILE)
    if state:
        proxy_addr = str(state.get("proxy_addr") or "").strip()
        if proxy_addr:
            return proxy_addr
    return DEVDNS_PROXY_ADDR


def _write_daemon_state(pid: int, proxy_addr: str) -> None:
    DAEMON_STATE_FILE.write_text(
        json.dumps({"pid": pid, "proxy_addr": proxy_addr}, indent=2),
        encoding="utf-8",
    )


def _clear_daemon_state() -> None:
    if DAEMON_STATE_FILE.exists():
        DAEMON_STATE_FILE.unlink()


def _is_bind_permission_denied(output: str) -> bool:
    lowered = output.lower()
    return "bind:" in lowered and "permission denied" in lowered


def _replace_port(addr: str, port: int) -> str:
    text = addr.strip()
    if not text:
        return f"127.0.0.1:{port}"
    if ":" not in text:
        return f"{text}:{port}"
    host, _sep, _current = text.rpartition(":")
    if not host:
        return f"127.0.0.1:{port}"
    return f"{host}:{port}"


def _declared_routes(partitions: Iterable[str] | None) -> list[DeclaredRoute]:
    selected = list(partitions or _partition_names())
    routes: list[DeclaredRoute] = []
    for partition in selected:
        intents_dir = PARTITIONS / partition / "intents"
        if not intents_dir.is_dir():
            continue
        for intent_path in sorted(intents_dir.glob("*.yaml")):
            doc = yaml.safe_load(intent_path.read_text(encoding="utf-8")) or {}
            if not isinstance(doc, dict) or doc.get("kind") != "Intent":
                continue
            spec = doc.get("spec") or {}
            assets = spec.get("assets") or []
            assets_by_name = {
                asset.get("name"): asset
                for asset in assets
                if isinstance(asset, dict) and asset.get("name")
            }
            target_pusher = str(spec.get("targetPusher") or "").strip().lower()
            if "k8s" not in target_pusher and "kubernetes" not in target_pusher:
                continue
            target = spec.get("target") or {}
            namespace = str(target.get("namespace") or "default").strip() or "default"
            cluster = str(target.get("cluster") or "").strip()
            intent_name = str((doc.get("metadata") or {}).get("name") or intent_path.stem).strip()

            for asset in assets:
                if not isinstance(asset, dict) or asset.get("type") != "DevDNSRoute":
                    continue
                props = asset.get("properties") or {}
                hostname = str(props.get("hostname") or "").strip()
                target_name = str(props.get("target") or "").strip()
                port_name = str(props.get("portName") or "").strip()
                asset_name = str(asset.get("name") or "").strip()
                target_asset = assets_by_name.get(target_name)
                if not hostname or not target_name or not asset_name or not isinstance(target_asset, dict):
                    warn(f"skipping invalid DevDNSRoute in {intent_path}")
                    continue
                try:
                    remote_port = _resolve_remote_port(target_asset, port_name)
                except ValueError as exc:
                    warn(f"skipping {partition}/{intent_name}/{asset_name}: {exc}")
                    continue
                service = _resource_name(
                    "k8s-svc-compute",
                    cluster,
                    namespace,
                    partition,
                    intent_name,
                    target_name,
                )
                routes.append(
                    DeclaredRoute(
                        partition=partition,
                        intent=intent_name,
                        asset=asset_name,
                        hostname=hostname,
                        namespace=namespace,
                        resource=f"service/{service}",
                        remote_port=remote_port,
                    )
                )
    return routes


def _resolve_remote_port(target_asset: dict, requested_port_name: str) -> str:
    ports = (target_asset.get("properties") or {}).get("ports") or []
    if requested_port_name:
        for port in ports:
            if str((port or {}).get("name") or "").strip() == requested_port_name:
                return requested_port_name
        raise ValueError(
            f"requested portName {requested_port_name!r} was not found on target {target_asset.get('name')}"
        )
    if len(ports) != 1:
        raise ValueError(
            f"target {target_asset.get('name')} exposes {len(ports)} ports; portName is required"
        )
    port = ports[0] or {}
    name = str(port.get("name") or "").strip()
    if name:
        return name
    for key in ("servicePort", "port", "containerPort"):
        value = port.get(key)
        if value not in (None, ""):
            return str(value)
    raise ValueError(f"target {target_asset.get('name')} does not declare a usable port")


def _resource_name(prefix: str, cluster: str, namespace: str, partition: str, intent: str, asset: str) -> str:
    parts = [prefix, cluster, namespace, "", "", partition, intent, asset]
    filtered = [_sanitize_part(part) for part in parts if _sanitize_part(part)]
    if not filtered:
        return "guardian"
    base = "-".join(filtered)
    if len(base) <= 63:
        return base
    digest = hashlib.sha256(json.dumps(filtered, separators=(",", ":")).encode("utf-8")).hexdigest()[:10]
    keep = max(1, 63 - 1 - len(digest))
    return base[:keep].strip("-") + "-" + digest


def _sanitize_part(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    out: list[str] = []
    prev_dash = False
    for char in text:
        is_alpha_num = ("a" <= char <= "z") or ("0" <= char <= "9")
        if is_alpha_num:
            out.append(char)
            prev_dash = False
            continue
        if not prev_dash:
            out.append("-")
            prev_dash = True
    sanitized = "".join(out).strip("-")
    return sanitized or "x"


def _ensure_route_process(route: DeclaredRoute, *, dry_run: bool) -> None:
    metadata_path = ROUTE_STATE_DIR / f"{route.route_id}.json"
    command = _route_command(route)
    current = _load_json(metadata_path)
    if current:
        pid = int(current.get("pid") or 0)
        if pid > 0 and _process_alive(pid) and current.get("command") == command:
            return
        _stop_route_meta(metadata_path, dry_run=dry_run)

    info("+ " + " ".join(command))
    if dry_run:
        return

    log_path = LOG_DIR / f"{route.route_id}.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(1)
    if proc.poll() is not None and proc.returncode != 0:
        tail = _tail(log_path)
        raise SystemExit(
            f"error: failed to start devdns route {route.hostname}. Check {log_path}. Last output:\n{tail}"
        )
    metadata = asdict(route)
    metadata.update({"pid": proc.pid, "command": command})
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _route_command(route: DeclaredRoute) -> list[str]:
    command = [
        str(DEVDNSCTL_BIN),
        "--server",
        DEVDNS_ADMIN_URL,
        "k8s",
        "port-forward",
        "--resource",
        route.resource,
        "--remote-port",
        route.remote_port,
        "--name",
        route.hostname,
        "--namespace",
        route.namespace,
    ]
    context = kubectl_context()
    if context:
        command.extend(["--context", context])
    return command


def _resource_exists(namespace: str, resource: str, *, dry_run: bool) -> bool:
    if dry_run:
        return True
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", resource],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _stop_route_meta(metadata_path: Path, *, dry_run: bool) -> None:
    current = _load_json(metadata_path)
    if not current:
        if metadata_path.exists() and not dry_run:
            metadata_path.unlink()
        return
    pid = int(current.get("pid") or 0)
    if pid > 0:
        info(f"stopping devdns route process {pid} from {metadata_path.name}")
        if not dry_run:
            _terminate_pid(pid)
    if not dry_run and metadata_path.exists():
        metadata_path.unlink()


def _stop_pid_file(pid_path: Path, *, dry_run: bool) -> None:
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        pid = 0
    if pid > 0:
        info(f"stopping process {pid} from {pid_path.name}")
        if not dry_run:
            _terminate_pid(pid)
    if not dry_run and pid_path.exists():
        pid_path.unlink()


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _partition_names() -> list[str]:
    parts: list[str] = []
    for entry in sorted(PARTITIONS.iterdir(), key=lambda path: path.name):
        if entry.is_dir() and not entry.name.startswith("_"):
            parts.append(entry.name)
    return parts


def _ensure_state_dirs(dry_run: bool) -> None:
    for path in (STATE_DIR, LOG_DIR, ROUTE_STATE_DIR):
        if dry_run:
            continue
        path.mkdir(parents=True, exist_ok=True)
    if not dry_run and not ROUTES_FILE.exists():
        ROUTES_FILE.write_text("[]\n", encoding="utf-8")


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _tail(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def _address_port(addr: str, *, default: int) -> int:
    text = addr.strip()
    if not text:
        return default
    if ":" not in text:
        try:
            return int(text)
        except ValueError:
            return default
    try:
        return int(text.rsplit(":", 1)[1])
    except ValueError:
        return default