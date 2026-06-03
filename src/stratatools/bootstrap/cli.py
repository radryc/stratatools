"""Bootstrap CLI: build|deploy|rollout|stop|destroy|stamp-urls."""
from __future__ import annotations

import base64
import binascii
import importlib
import os
import secrets
import subprocess
from pathlib import Path

import typer

from stratatools.monofs_key import ensure_monofs_encryption_key
from stratatools.util import PARTITIONS, ROOT, die, info, run, warn
from . import storage, guardian

app = typer.Typer(
    no_args_is_help=False,
    help="Bootstrap MonoFS storage + Guardian control plane.",
)

METRICS_URL = (
    "https://github.com/kubernetes-sigs/metrics-server/releases/latest/"
    "download/components.yaml"
)
AINFRA = ROOT.parent
LOCAL_BIN_DIR = Path(
    os.environ.get("STRATATOOLS_BIN_DIR", str(Path.home() / "bin"))
).expanduser()
GUARDIAN_REPO_DIR = Path(os.environ.get("GUARDIAN_REPO_DIR", str(AINFRA / "guardian")))
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", str(AINFRA / "monofs")))
BOOTSTRAP_ENV_FILE = Path(
    os.environ.get("STRATATOOLS_BOOTSTRAP_ENV", str(ROOT / "bootstrap.local.env"))
).expanduser()
LOCAL_BIN_BUILD_TARGETS = [
    ("guardianctl", GUARDIAN_REPO_DIR, "./cmd/guardianctl"),
    ("monofs-client", MONOFS_REPO_DIR, "./cmd/monofs-client"),
    ("monofs-session", MONOFS_REPO_DIR, "./cmd/monofs-session"),
    ("monofs-search", MONOFS_REPO_DIR, "./cmd/monofs-search"),
]


def _load_bootstrap_env() -> None:
    if not BOOTSTRAP_ENV_FILE.exists():
        return
    try:
        lines = BOOTSTRAP_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        warn(f"unable to read bootstrap env file {BOOTSTRAP_ENV_FILE}: {exc}")
        return

    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        os.environ[key] = value
        loaded += 1

    if loaded:
        info(f"loaded {loaded} bootstrap env values from {BOOTSTRAP_ENV_FILE}")


def _reload_bootstrap_modules() -> None:
    # Bootstrap modules compute runtime defaults at import time.
    # Reload after loading bootstrap.local.env so command execution picks up overrides.
    importlib.reload(storage)
    importlib.reload(guardian)


def _read_secret_key(namespace: str, secret_name: str, key: str) -> str:
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "secret",
            secret_name,
            "-o",
            f"jsonpath={{.data.{key}}}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode()
    except (binascii.Error, UnicodeDecodeError):
        warn(
            f"unable to decode {namespace}/{secret_name}:{key}; generating a new value"
        )
        return ""


def _ensure_secret_env(
    env_name: str,
    namespace: str,
    secret_name: str,
    key: str,
    *,
    dry_run: bool,
) -> None:
    if os.environ.get(env_name, "").strip():
        return

    value = ""
    if not dry_run:
        value = _read_secret_key(namespace, secret_name, key)
        if value:
            info(f"reusing {env_name} from {namespace}/{secret_name}")

    if not value:
        value = secrets.token_urlsafe(32)
        info(f"generated {env_name} for bootstrap")

    os.environ[env_name] = value


def _ensure_bootstrap_secrets(dry_run: bool) -> None:
    try:
        key_state = ensure_monofs_encryption_key(
            repo_dir=MONOFS_REPO_DIR,
            dry_run=dry_run,
            create_if_missing=dry_run,
        )
    except (FileNotFoundError, ValueError) as exc:
        die(f"{exc}. Run `st-setup` before bootstrap.")
    if key_state.created:
        action = "would ensure" if dry_run else "ensured"
        info(f"{action} MONOFS_ENCRYPTION_KEY in {key_state.env_file} for bootstrap")

    _ensure_secret_env(
        "MONOFS_TOKEN",
        storage.NAMESPACE,
        "monofs-secrets",
        "monofs-token",
        dry_run=dry_run,
    )
    _ensure_secret_env(
        "CLIENT_DISCOVERY_TOKEN",
        guardian.NAMESPACE,
        "guardian-secrets",
        "client-discovery-token",
        dry_run=dry_run,
    )


def _path_contains(path: Path) -> bool:
    target = path.resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).expanduser().resolve() == target:
                return True
        except OSError:
            continue
    return False


def _install_local_bins(dry_run: bool) -> None:
    for _name, repo_dir, _target in LOCAL_BIN_BUILD_TARGETS:
        if not repo_dir.is_dir():
            die(
                f"required repo not found: {repo_dir}. "
                "Run `st-setup` first or set GUARDIAN_REPO_DIR / MONOFS_REPO_DIR."
            )

    info(f"=== building local CLIs into {LOCAL_BIN_DIR} ===")
    info(f"+ mkdir -p {LOCAL_BIN_DIR}")
    if not dry_run:
        LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)

    for name, repo_dir, target in LOCAL_BIN_BUILD_TARGETS:
        run(
            ["go", "build", "-o", str(LOCAL_BIN_DIR / name), target],
            cwd=repo_dir,
            dry_run=dry_run,
        )

    if not dry_run and not _path_contains(LOCAL_BIN_DIR):
        warn(
            f"{LOCAL_BIN_DIR} is not on PATH; add it or set GUARDIANCTL_BIN="
            f"{LOCAL_BIN_DIR / 'guardianctl'}"
        )


def _install_prereqs(dry_run: bool) -> None:
    run(["kubectl", "apply", "-f", METRICS_URL], check=False, dry_run=dry_run)
    run(
        [
            "kubectl",
            "-n",
            "kube-system",
            "patch",
            "deployment",
            "metrics-server",
            "--type=json",
            "-p",
            '[{"op":"add","path":"/spec/template/spec/containers/0/args/-",'
            '"value":"--kubelet-insecure-tls"}]',
        ],
        check=False,
        dry_run=dry_run,
    )
    for rbac in [
        PARTITIONS / "guardian-configs" / "guardian-configs-sa-cluster-admin.yaml",
        PARTITIONS / "opentelemetry" / "collector-prometheus-rbac-default-sa.yaml",
        PARTITIONS / "k8s-top" / "metrics-reader-rbac-default-sa.yaml",
        PARTITIONS / "dev-workspace" / "default-sa-cluster-admin.yaml",
    ]:
        if rbac.exists():
            run(["kubectl", "apply", "-f", str(rbac)], check=False, dry_run=dry_run)


@app.command()
def build(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs plus MonoFS + Guardian images."""
    _load_bootstrap_env()
    _reload_bootstrap_modules()
    guardian.sync_local_aws_intent(dry_run)
    _install_local_bins(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)


@app.command()
def deploy(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs and bootstrap images, then deploy storage + Guardian."""
    _load_bootstrap_env()
    _reload_bootstrap_modules()
    guardian.sync_local_aws_intent(dry_run)
    _install_local_bins(dry_run)
    _ensure_bootstrap_secrets(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)
    storage.load_images(dry_run)
    guardian.load_images(dry_run)
    storage.deploy(dry_run)
    guardian.deploy(dry_run)
    _install_prereqs(dry_run)


@app.command()
def rollout(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs, rebuild images, and restart deployments."""
    _load_bootstrap_env()
    _reload_bootstrap_modules()
    guardian.sync_local_aws_intent(dry_run)
    _install_local_bins(dry_run)
    _ensure_bootstrap_secrets(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)
    storage.load_images(dry_run)
    guardian.load_images(dry_run)
    storage.rollout(dry_run)
    guardian.rollout(dry_run)


@app.command()
def stop(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Scale deployments to 0 (Guardian first, then storage)."""
    guardian.stop(dry_run)
    storage.stop(dry_run)


@app.command()
def destroy(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Delete Guardian + storage namespaces."""
    guardian.destroy(dry_run)
    storage.destroy(dry_run)


@app.command("stamp-urls")
def stamp_urls(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Resolve external URLs/endpoints and stamp them into partition YAMLs."""
    _load_bootstrap_env()
    _reload_bootstrap_modules()
    guardian.stamp_urls(dry_run)
