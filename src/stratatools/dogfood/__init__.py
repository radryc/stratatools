"""st-dogfood — ingest the local Strata repo set into MonoFS."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import os
import re
import subprocess
from pathlib import Path
import time

import typer

from stratatools.monofs_key import (
    ensure_monofs_encryption_key,
    is_valid_monofs_encryption_key,
    monofs_repo_dir,
)
from stratatools.setup import REPOS as SIBLING_REPOS
from stratatools.util import ROOT, die, info, run, warn

app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="Ingest the local Strata repository set into MonoFS.",
)

AINFRA = ROOT.parent
MONOFS_REPO_DIR = monofs_repo_dir()
LOCAL_BIN_DIR = Path(
    os.environ.get("STRATATOOLS_BIN_DIR", str(Path.home() / "bin"))
).expanduser()
DEFAULT_ROUTER = os.environ.get("MONOFS_ROUTER", "localhost:9090")
GITHUB_SSH_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"),
)
LOCAL_ROUTER_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOCAL_MONOFS_COMPOSE_SERVICES = ("fetcher-a", "fetcher-b", "router-a", "router-b")
LOCAL_MONOFS_DEV_STATE_DIR = Path("/tmp/monofs-dev")
MONOFS_BOOTSTRAP_NAMESPACE = os.environ.get("MONOFS_NAMESPACE", "monofs")
MONOFS_BOOTSTRAP_SECRET = "monofs-secrets"
BOOTSTRAP_ROUTER_DEPLOYMENTS = ("router-a", "router-b")
BOOTSTRAP_CLUSTER_DEPLOYMENTS = (
    "router-a",
    "router-b",
    "fetcher-a",
    "fetcher-b",
    "node-a",
    "node-b",
    "node-c",
    "node-d",
    "node-e",
)
DOGFOOD_EXCLUDED_REPOS = {"agent"}


@dataclass(frozen=True)
class RepoIngestSpec:
    name: str
    path: Path
    source: str
    ref: str


def _capture(cmd: list[str], *, cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _normalize_git_source(source: str) -> str:
    source = source.strip()
    for pattern in GITHUB_SSH_PATTERNS:
        match = pattern.match(source)
        if match:
            return f"https://github.com/{match.group('owner')}/{match.group('repo')}.git"
    return source


def _resolve_ref(repo_dir: Path) -> str:
    try:
        ref = _capture(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo_dir)
    except RuntimeError:
        ref = ""
    if ref:
        return ref
    return _capture(["git", "rev-parse", "HEAD"], cwd=repo_dir)


def _ensure_checkout_monofs_encryption_key() -> tuple[str, bool]:
    try:
        state = ensure_monofs_encryption_key(
            repo_dir=MONOFS_REPO_DIR,
            create_if_missing=False,
        )
    except FileNotFoundError:
        die(
            "MONOFS_ENCRYPTION_KEY is not configured. Run `st-setup` to create "
            f"{MONOFS_REPO_DIR / '.env'} before using st-bootstrap or st-dogfood."
        )
    except ValueError as exc:
        die(f"{exc}. Fix the configured key or rerun `st-setup` before using st-dogfood.")

    return state.key, state.created


def _router_host(router: str) -> str:
    value = router.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def _is_local_router(router: str) -> bool:
    return _router_host(router) in LOCAL_ROUTER_HOSTS


def _detect_running_compose_services() -> list[str]:
    if not MONOFS_REPO_DIR.is_dir():
        return []

    result = subprocess.run(
        ["docker", "compose", "ps", "--services", "--status", "running"],
        cwd=MONOFS_REPO_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return [service for service in LOCAL_MONOFS_COMPOSE_SERVICES if service in running]


def _has_local_dev_runtime() -> bool:
    if not LOCAL_MONOFS_DEV_STATE_DIR.exists():
        return False
    result = subprocess.run(
        ["pgrep", "-f", "monofs-(router|server|client)"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _restart_local_monofs_runtime(compose_services: list[str], has_local_dev_runtime: bool) -> str:
    if compose_services:
        info(
            "restarting local MonoFS compose services: "
            + ", ".join(compose_services)
        )
        run(["docker", "compose", "restart", *compose_services], cwd=MONOFS_REPO_DIR)
        return "docker compose"

    if has_local_dev_runtime:
        info("restarting local MonoFS dev runtime")
        run(["make", "deploy-stop"], cwd=MONOFS_REPO_DIR)
        run(["make", "deploy-local"], cwd=MONOFS_REPO_DIR)
        return "make deploy-local"

    return ""


def _kubectl_capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True)


def _bootstrap_cluster_available() -> bool:
    namespace_result = _kubectl_capture(["get", "namespace", MONOFS_BOOTSTRAP_NAMESPACE])
    if namespace_result.returncode != 0:
        return False
    for deployment in BOOTSTRAP_ROUTER_DEPLOYMENTS:
        result = _kubectl_capture(
            ["-n", MONOFS_BOOTSTRAP_NAMESPACE, "get", "deployment", deployment]
        )
        if result.returncode != 0:
            return False
    return True


def _read_bootstrap_cluster_secret_key() -> str:
    result = _kubectl_capture(
        [
            "-n",
            MONOFS_BOOTSTRAP_NAMESPACE,
            "get",
            "secret",
            MONOFS_BOOTSTRAP_SECRET,
            "-o",
            "jsonpath={.data.monofs-encryption-key}",
        ]
    )
    if result.returncode != 0:
        return ""
    raw = result.stdout.strip()
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode()
    except (binascii.Error, UnicodeDecodeError):
        return ""


def _bootstrap_router_missing_env(deployment: str) -> bool:
    result = _kubectl_capture(
        [
            "-n",
            MONOFS_BOOTSTRAP_NAMESPACE,
            "get",
            "deployment",
            deployment,
            "-o",
            "jsonpath={range .spec.template.spec.containers[0].env[*]}{.name}{" + "\n" + "}{end}",
        ]
    )
    if result.returncode != 0:
        return False
    env_names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return "MONOFS_ENCRYPTION_KEY" not in env_names


def _patch_bootstrap_cluster_secret_key(key: str) -> None:
    payload = json.dumps(
        {"data": {"monofs-encryption-key": base64.b64encode(key.encode()).decode()}}
    )
    run(
        [
            "kubectl",
            "-n",
            MONOFS_BOOTSTRAP_NAMESPACE,
            "patch",
            "secret",
            MONOFS_BOOTSTRAP_SECRET,
            "--type=merge",
            "-p",
            payload,
        ]
    )


def _patch_bootstrap_router_env(deployment: str) -> None:
    payload = json.dumps(
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "router",
                                "env": [
                                    {
                                        "name": "MONOFS_ENCRYPTION_KEY",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": MONOFS_BOOTSTRAP_SECRET,
                                                "key": "monofs-encryption-key",
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
    )
    run(
        [
            "kubectl",
            "-n",
            MONOFS_BOOTSTRAP_NAMESPACE,
            "patch",
            "deployment",
            deployment,
            "--type=strategic",
            "-p",
            payload,
        ]
    )


def _restart_bootstrap_cluster_deployments(deployments: tuple[str, ...]) -> None:
    run(
        [
            "kubectl",
            "-n",
            MONOFS_BOOTSTRAP_NAMESPACE,
            "rollout",
            "restart",
            *[f"deployment/{deployment}" for deployment in deployments],
        ]
    )
    for deployment in deployments:
        run(
            [
                "kubectl",
                "-n",
                MONOFS_BOOTSTRAP_NAMESPACE,
                "rollout",
                "status",
                f"deployment/{deployment}",
                "--timeout=180s",
            ]
        )


def _repair_bootstrap_monofs_runtime(monofs_admin: Path, router: str, *, dry_run: bool) -> bool:
    if dry_run or not _bootstrap_cluster_available():
        return False

    cluster_key = _read_bootstrap_cluster_secret_key()
    secret_changed = False
    if not is_valid_monofs_encryption_key(cluster_key):
        cluster_key, _ = _ensure_checkout_monofs_encryption_key()
        info(
            f"updating {MONOFS_BOOTSTRAP_NAMESPACE}/{MONOFS_BOOTSTRAP_SECRET} "
            "with MONOFS_ENCRYPTION_KEY"
        )
        _patch_bootstrap_cluster_secret_key(cluster_key)
        secret_changed = True

    os.environ["MONOFS_ENCRYPTION_KEY"] = cluster_key

    routers_missing_env = tuple(
        deployment
        for deployment in BOOTSTRAP_ROUTER_DEPLOYMENTS
        if _bootstrap_router_missing_env(deployment)
    )
    for deployment in routers_missing_env:
        info(
            f"patching {MONOFS_BOOTSTRAP_NAMESPACE}/{deployment} with MONOFS_ENCRYPTION_KEY"
        )
        _patch_bootstrap_router_env(deployment)

    if not secret_changed and not routers_missing_env:
        return False

    deployments_to_restart = (
        BOOTSTRAP_CLUSTER_DEPLOYMENTS if secret_changed else routers_missing_env
    )
    _restart_bootstrap_cluster_deployments(deployments_to_restart)
    if not _wait_for_router_ready(monofs_admin, router):
        die(
            f"repaired bootstrap MonoFS runtime in namespace {MONOFS_BOOTSTRAP_NAMESPACE}, "
            f"but {router} did not become ready again."
        )

    info(
        f"bootstrap MonoFS runtime in namespace {MONOFS_BOOTSTRAP_NAMESPACE} now has "
        "MONOFS_ENCRYPTION_KEY wired into the router"
    )
    return True


def _wait_for_router_ready(monofs_admin: Path, router: str, *, timeout_seconds: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [str(monofs_admin), "status", f"--router={router}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(2)
    return False


def _ensure_local_monofs_encryption_key(monofs_admin: Path, router: str, *, dry_run: bool) -> None:
    if dry_run or not _is_local_router(router):
        return

    compose_services = _detect_running_compose_services()
    has_local_dev_runtime = _has_local_dev_runtime()
    if compose_services or has_local_dev_runtime:
        _ensure_checkout_monofs_encryption_key()
        restart_mode = _restart_local_monofs_runtime(compose_services, has_local_dev_runtime)
        if not _wait_for_router_ready(monofs_admin, router):
            die(
                f"restarted {restart_mode} with updated MONOFS_ENCRYPTION_KEY, but {router} "
                "did not become ready again."
            )
        info(f"local MonoFS runtime restarted with updated MONOFS_ENCRYPTION_KEY via {restart_mode}")
        return

    bootstrap_runtime = _bootstrap_cluster_available()
    if _repair_bootstrap_monofs_runtime(monofs_admin, router, dry_run=dry_run):
        return

    if bootstrap_runtime:
        return

    _ensure_checkout_monofs_encryption_key()
    warn(
        "generated MONOFS_ENCRYPTION_KEY automatically, but no restartable local MonoFS runtime "
        "or bootstrap cluster deployment was detected. If localhost:9090 is backed by a different "
        "runtime, it still needs to pick up the new key before ingest will succeed."
    )


def _preflight_local_blob_ingest(monofs_admin: Path, router: str, *, dry_run: bool) -> None:
    _ensure_local_monofs_encryption_key(monofs_admin, router, dry_run=dry_run)


def _discover_repos() -> list[RepoIngestSpec]:
    inventory: list[tuple[str, Path]] = [("stratatools", ROOT)]
    inventory.extend((name, AINFRA / name) for name in SIBLING_REPOS)

    repos: list[RepoIngestSpec] = []
    seen: set[str] = set()
    for name, repo_dir in inventory:
        if name in seen:
            continue
        seen.add(name)

        if name in DOGFOOD_EXCLUDED_REPOS:
            info(f"skipping repo {name}: excluded from st-dogfood")
            continue

        if not repo_dir.is_dir():
            warn(f"skipping missing repo {name}: {repo_dir}")
            continue
        if not (repo_dir / ".git").exists():
            warn(f"skipping non-git repo {name}: {repo_dir}")
            continue

        try:
            source = _normalize_git_source(
                _capture(["git", "remote", "get-url", "origin"], cwd=repo_dir)
            )
            ref = _resolve_ref(repo_dir)
        except RuntimeError as exc:
            warn(f"skipping repo {name}: {exc}")
            continue

        repos.append(RepoIngestSpec(name=name, path=repo_dir, source=source, ref=ref))

    return repos


def _resolve_monofs_admin(*, build: bool, dry_run: bool) -> Path:
    configured = os.environ.get("MONOFS_ADMIN_BIN", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            LOCAL_BIN_DIR / "monofs-admin",
            MONOFS_REPO_DIR / "bin" / "monofs-admin",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    if not build:
        die(
            "monofs-admin not found. Set MONOFS_ADMIN_BIN, build ../monofs/bin/monofs-admin, "
            "or rerun without --no-build."
        )
    if not MONOFS_REPO_DIR.is_dir():
        die(
            f"monofs repo not found: {MONOFS_REPO_DIR}. "
            "Run `st-setup` first or set MONOFS_REPO_DIR."
        )

    target = LOCAL_BIN_DIR / "monofs-admin"
    info(f"building monofs-admin into {target}")
    if not dry_run:
        LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    run(["go", "build", "-o", str(target), "./cmd/monofs-admin"], cwd=MONOFS_REPO_DIR, dry_run=dry_run)
    return target


@app.callback(invoke_without_command=True)
def main(
    router: str = typer.Option(
        DEFAULT_ROUTER, "--router", help="MonoFS router gRPC address."
    ),
    build: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Build monofs-admin into STRATATOOLS_BIN_DIR when it is missing.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands only."),
) -> None:
    """Ingest stratatools and its sibling Strata repositories into MonoFS."""
    monofs_admin = _resolve_monofs_admin(build=build, dry_run=dry_run)
    repos = _discover_repos()
    if not repos:
        die("no local Strata repositories found to ingest")
    _preflight_local_blob_ingest(monofs_admin, router, dry_run=dry_run)

    info(f"dogfooding MonoFS from {len(repos)} local repositories")
    failures: list[str] = []
    ingested: list[str] = []
    for repo in repos:
        info(f"=== ingest repo: {repo.name} ({repo.ref}) ===")
        result = run(
            [
                str(monofs_admin),
                "ingest",
                f"--router={router}",
                f"--source={repo.source}",
                f"--ref={repo.ref}",
            ],
            check=False,
            cwd=repo.path,
            dry_run=dry_run,
        )
        if result and result.returncode != 0:
            failures.append(repo.name)
            warn(f"ingest failed for {repo.name} (exit {result.returncode})")
            continue
        ingested.append(repo.name)

    if failures:
        die(f"dogfood ingest failed for: {', '.join(failures)}")

    info(f"dogfood ingest complete: {ingested}")


__all__ = ["app"]