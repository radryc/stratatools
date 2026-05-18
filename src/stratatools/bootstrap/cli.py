"""Bootstrap CLI: build|deploy|rollout|stop|destroy|stamp-urls."""
from __future__ import annotations

import os
from pathlib import Path

import typer

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
LOCAL_BIN_BUILD_TARGETS = [
    ("guardianctl", GUARDIAN_REPO_DIR, "./cmd/guardianctl"),
    ("monofs-client", MONOFS_REPO_DIR, "./cmd/monofs-client"),
    ("monofs-session", MONOFS_REPO_DIR, "./cmd/monofs-session"),
    ("monofs-search", MONOFS_REPO_DIR, "./cmd/monofs-search"),
]


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
        PARTITIONS / "opentelemetry" / "collector-prometheus-rbac-default-sa.yaml",
        PARTITIONS / "k8s-top" / "metrics-reader-rbac-default-sa.yaml",
        PARTITIONS / "dev-workspace" / "default-sa-cluster-admin.yaml",
    ]:
        if rbac.exists():
            run(["kubectl", "apply", "-f", str(rbac)], check=False, dry_run=dry_run)


@app.command()
def build(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs plus MonoFS + Guardian images."""
    _install_local_bins(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)


@app.command()
def deploy(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs and bootstrap images, then deploy storage + Guardian."""
    _install_local_bins(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)
    storage.deploy(dry_run)
    guardian.deploy(dry_run)
    _install_prereqs(dry_run)


@app.command()
def rollout(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Build local CLIs, rebuild images, and restart deployments."""
    _install_local_bins(dry_run)
    storage.build_images(dry_run)
    guardian.build_images(dry_run)
    storage.rollout(dry_run)
    guardian.deploy(dry_run)


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
    """Resolve external LB URL and stamp into partition YAMLs."""
    guardian.stamp_urls(dry_run)
