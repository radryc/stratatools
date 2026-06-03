"""st-setup — clone sibling repos and run prerequisite checks."""
from __future__ import annotations

from datetime import datetime
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

from stratatools.monofs_key import ensure_monofs_encryption_key
from stratatools.util import ROOT, info, run

PARTITIONS_DIR = ROOT / "partitions"
PARTITION_TEMPLATE_DIR = PARTITIONS_DIR / "_template"

REPOS: dict[str, str] = {
    "guardian": "https://github.com/radryc/guardian.git",
    "doctor":   "https://github.com/radryc/doctor.git",
    "monofs":   "https://github.com/radryc/monofs.git",
    "kvs":      "https://github.com/radryc/kvs.git",
    "k8s-top":  "https://github.com/radryc/k8s-top.git",
    "agent":    "https://github.com/radryc/agent.git",
    "packager": "https://github.com/radryc/packager.git",
    "cfg":      "https://github.com/radryc/cfg.git",
}

INSTALL_HINTS: dict[str, dict[str, str]] = {
    "docker": {
        "darwin": "Install Docker Desktop for Mac:\n  https://www.docker.com/products/docker-desktop/\n  Or via Homebrew: brew install --cask docker",
        "linux-ubuntu": "Install Docker Desktop for Linux:\n  https://docs.docker.com/desktop/install/linux/ubuntu/\nOr Docker Engine (CLI only):\n  curl -fsSL https://get.docker.com | sh\n  sudo usermod -aG docker $USER  # then log out + back in",
        "linux-fedora": "Install Docker Desktop for Linux:\n  https://docs.docker.com/desktop/install/linux/fedora/\nOr Docker Engine:\n  sudo dnf -y install dnf-plugins-core\n  sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo\n  sudo dnf install -y docker-ce docker-ce-cli containerd.io\n  sudo systemctl enable --now docker\n  sudo usermod -aG docker $USER",
        "linux-arch": "  sudo pacman -S docker docker-buildx\n  sudo systemctl enable --now docker\n  sudo usermod -aG docker $USER",
        "wsl": "On WSL2, install Docker Desktop on Windows and enable WSL2 integration:\n  https://docs.docker.com/desktop/wsl/",
        "windows": "Install Docker Desktop for Windows:\n  https://www.docker.com/products/docker-desktop/",
    },
    "go": {
        "darwin": "brew install go\nOr download from https://go.dev/dl/",
        "linux-ubuntu": "Recommended (official tarball, latest):\n  wget https://go.dev/dl/go1.22.5.linux-amd64.tar.gz\n  sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf go1.22.5.linux-amd64.tar.gz\n  echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc\nOr apt (may be older): sudo apt install golang-go",
        "linux-fedora": "sudo dnf install -y golang\nOr from https://go.dev/dl/",
        "linux-arch": "sudo pacman -S go",
        "wsl": "Same as linux-ubuntu (above)",
        "windows": "Download from https://go.dev/dl/",
    },
    "kubectl": {
        "darwin": "brew install kubectl",
        "linux-ubuntu": "  curl -LO https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl\n  sudo install -m 0755 kubectl /usr/local/bin/kubectl",
        "linux-fedora": "sudo dnf install -y kubectl",
        "linux-arch": "sudo pacman -S kubectl",
        "wsl": "Same as linux-ubuntu",
        "windows": "choco install kubernetes-cli\nOr from https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/",
    },
    "uv": {
        "*": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "windows": "powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"",
    },
    "git": {
        "darwin": "brew install git\nOr install Xcode Command Line Tools: xcode-select --install",
        "linux-ubuntu": "sudo apt install -y git",
        "linux-fedora": "sudo dnf install -y git",
        "linux-arch": "sudo pacman -S git",
        "wsl": "sudo apt install -y git",
        "windows": "Download from https://git-scm.com/download/win",
    },
    "make": {
        "darwin": "xcode-select --install",
        "linux-ubuntu": "sudo apt install -y build-essential",
        "linux-fedora": "sudo dnf groupinstall -y 'Development Tools'",
        "linux-arch": "sudo pacman -S base-devel",
        "wsl": "sudo apt install -y build-essential",
        "windows": "choco install make",
    },
    "python3": {
        "darwin": "brew install python@3.11",
        "linux-ubuntu": "sudo apt install -y python3 python3-venv python3-pip",
        "linux-fedora": "sudo dnf install -y python3 python3-pip",
        "linux-arch": "sudo pacman -S python python-pip",
        "wsl": "sudo apt install -y python3 python3-venv python3-pip",
        "windows": "Download from https://www.python.org/downloads/",
    },
    "guardianctl": {
        "*": "Built by `st-bootstrap build|deploy|rollout` into ~/bin.\nManual fallback:\n  cd ../guardian && go build -o ~/bin/guardianctl ./cmd/guardianctl\nOr skip — guardianctl is only required for `st-release`.",
    },
}

CLUSTER_HINT = [
    "Kubernetes cluster is unreachable. Options:",
    "  - Start Docker Desktop and enable Kubernetes (Settings → Kubernetes → Enable)",
    "  - Install kind:  go install sigs.k8s.io/kind@latest && kind create cluster",
    "  - Install minikube: https://minikube.sigs.k8s.io/docs/start/",
    "  - Point KUBECONFIG at a remote cluster",
]
KIND_INSTALL_TARGET = "sigs.k8s.io/kind@latest"
DEFAULT_KIND_CLUSTER = "strata"
DEFAULT_KIND_WORKERS = 5

app = typer.Typer(no_args_is_help=False, help="bootstrap sibling repos and verify host prerequisites")


def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _mark(state: str) -> str:
    """state: ok | fail | opt"""
    if _isatty():
        return {"ok": "\033[32m✓\033[0m", "fail": "\033[31m✗\033[0m", "opt": "-"}[state]
    return {"ok": "[ok]  ", "fail": "[FAIL]", "opt": "[--]  "}[state]


def _platform_key() -> str:
    sysname = platform.system().lower()
    if sysname == "darwin":
        return "darwin"
    if sysname == "windows":
        return "windows"
    if sysname == "linux":
        try:
            rel = os.uname().release.lower()
        except Exception:
            rel = ""
        if "microsoft" in rel:
            return "wsl"
        try:
            data = Path("/etc/os-release").read_text()
            m = re.search(r"^ID=\"?([^\"\n]+)", data, re.M)
            distro = (m.group(1) if m else "").lower()
        except Exception:
            distro = ""
        if distro in ("ubuntu", "debian"):
            return "linux-ubuntu"
        if distro in ("fedora", "rhel", "centos", "rocky", "almalinux"):
            return "linux-fedora"
        if distro in ("arch", "manjaro", "endeavouros"):
            return "linux-arch"
        return "linux-ubuntu"
    return "linux-ubuntu"


def _hints_for(tool: str, pkey: str) -> list[str]:
    spec = INSTALL_HINTS.get(tool, {})
    text = spec.get(pkey) or spec.get("*") or "(no install hint available)"
    return text.splitlines()


def _try_version(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False, ""
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout) else ""
    out = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    return True, out


def _go_bin_dir() -> Path:
    gobin = os.environ.get("GOBIN", "").strip()
    if gobin:
        return Path(gobin).expanduser()
    gopath = os.environ.get("GOPATH", "").strip()
    if gopath:
        return Path(gopath).expanduser() / "bin"
    return Path.home() / "go" / "bin"


def _kind_binary() -> str | None:
    binary = shutil.which("kind")
    if binary:
        return binary
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    candidate = _go_bin_dir() / f"kind{suffix}"
    return str(candidate) if candidate.is_file() else None


def _kind_config(worker_count: int) -> str:
    node_extra = (
        "    extraMounts:\n"
        "    - hostPath: /var/run/docker.sock\n"
        "      containerPath: /var/run/docker.sock\n"
    )
    lines = [
        "kind: Cluster",
        "apiVersion: kind.x-k8s.io/v1alpha4",
        "nodes:",
        "  - role: control-plane",
    ]
    lines.append(node_extra.rstrip())
    for _ in range(worker_count):
        lines.append("  - role: worker")
        lines.append(node_extra.rstrip())
    return "\n".join(lines) + "\n"


def _ensure_kind_binary(dry_run: bool) -> str | None:
    binary = _kind_binary()
    if binary:
        return binary

    info("kind not found on PATH; installing it with `go install`")
    result = run(["go", "install", KIND_INSTALL_TARGET], check=False, dry_run=dry_run)
    if dry_run:
        return str(_go_bin_dir() / "kind")
    if result is None or result.returncode != 0:
        return None
    return _kind_binary()


def _kind_cluster_exists(kind_bin: str, name: str) -> bool:
    result = subprocess.run(
        [kind_bin, "get", "clusters"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _ensure_kind_cluster(name: str, worker_count: int, *, dry_run: bool) -> bool:
    kind_bin = _ensure_kind_binary(dry_run)
    if not kind_bin:
        return False

    if dry_run:
        info(f"+ {kind_bin} create cluster --name {name} --config <generated>")
        info(f"+ {kind_bin} export kubeconfig --name {name}")
        return True

    if _kind_cluster_exists(kind_bin, name):
        info(f"kind cluster '{name}' already exists; exporting kubeconfig")
    else:
        info(
            f"creating kind cluster '{name}' with 1 control-plane and {worker_count} workers"
        )
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".yaml", delete=False
        ) as handle:
            handle.write(_kind_config(worker_count))
            config_path = Path(handle.name)
        try:
            result = run(
                [kind_bin, "create", "cluster", "--name", name, "--config", str(config_path)],
                check=False,
            )
            if result is None or result.returncode != 0:
                return False
        finally:
            config_path.unlink(missing_ok=True)

    result = run([kind_bin, "export", "kubeconfig", "--name", name], check=False)
    return bool(result and result.returncode == 0)


def _check_cluster_reachability(
    missing: list[str],
    *,
    auto_kind: bool,
    kind_name: str,
    kind_workers: int,
    dry_run: bool,
    allow_create: bool,
) -> bool:
    if "kubectl" in missing:
        info(_row(_mark("fail"), "cluster", "kubectl missing"))
        return False

    cluster_ok, _ = _try_version(["kubectl", "cluster-info"])
    if cluster_ok:
        info(_row(_mark("ok"), "cluster", "reachable"))
        return True

    info(_row(_mark("fail"), "cluster", "unreachable"))
    can_auto_kind = auto_kind and allow_create and "docker" not in missing and "go" not in missing
    if not can_auto_kind:
        return False

    if dry_run:
        info(
            _row(
                _mark("opt"),
                "kind cluster",
                f"would create {kind_name} (1 control-plane + {kind_workers} workers)",
            )
        )
        return False

    kind_ok = _ensure_kind_cluster(kind_name, kind_workers, dry_run=False)
    if not kind_ok:
        info(_row(_mark("fail"), "kind cluster", f"{kind_name} provision failed"))
        return False

    cluster_ok, _ = _try_version(["kubectl", "cluster-info"])
    if cluster_ok:
        info(
            _row(
                _mark("ok"),
                "kind cluster",
                f"{kind_name} (1 control-plane + {kind_workers} workers)",
            )
        )
        info(_row(_mark("ok"), "cluster", f"reachable via kind-{kind_name}"))
        return True

    info(_row(_mark("fail"), "cluster", "still unreachable after kind provisioning"))
    return False


def _parse_go_version(s: str) -> tuple[int, int] | None:
    m = re.search(r"go(\d+)\.(\d+)", s)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _row(mark: str, name: str, detail: str) -> str:
    return f"{mark} {name:<14}{detail}"


def _clone_all(parent_dir: Path, names: list[str], dry_run: bool) -> list[tuple[str, bool, Path]]:
    parent_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool, Path]] = []
    for name in names:
        dest = parent_dir / name
        if dest.exists():
            results.append((name, True, dest))
            continue
        if dry_run:
            info(f"+ git clone {REPOS[name]} {dest}")
            results.append((name, False, dest))
            continue
        r = run(["git", "clone", REPOS[name], str(dest)], check=False)
        results.append((name, bool(r and r.returncode == 0), dest))
    return results


def _validate_partition_name(name: str) -> str:
    value = name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", value):
        raise typer.BadParameter(
            "expected lowercase letters, digits, and hyphens, starting with a letter"
        )
    return value


def _default_partition_version() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


@app.command("new-partition")
def new_partition(
    name: str = typer.Argument(..., callback=_validate_partition_name),
    version: str = typer.Option(
        "", "--version", help="Version stamp to substitute for __VERSION__."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing target partition directory."
    ),
) -> None:
    """Scaffold a new partition from partitions/_template/."""
    if not PARTITION_TEMPLATE_DIR.is_dir():
        raise typer.BadParameter(f"template directory missing: {PARTITION_TEMPLATE_DIR}")

    target_dir = PARTITIONS_DIR / name
    if target_dir.exists():
        if not force:
            raise typer.BadParameter(
                f"target already exists: {target_dir} (use --force to overwrite)"
            )
        shutil.rmtree(target_dir)

    resolved_version = version.strip() or _default_partition_version()

    info(f"scaffolding partition '{name}' (version {resolved_version}) at {target_dir}")
    shutil.copytree(PARTITION_TEMPLATE_DIR, target_dir)

    for path in sorted(target_dir.rglob("*__PARTITION__*"), reverse=True):
        path.rename(path.with_name(path.name.replace("__PARTITION__", name)))

    for file_path in target_dir.rglob("*"):
        if not file_path.is_file():
            continue
        text = file_path.read_text()
        text = text.replace("__PARTITION__", name).replace("__VERSION__", resolved_version)
        file_path.write_text(text)

    info("")
    info(f"Created {target_dir}.")
    info("")
    info("Next steps:")
    info(f"  1. Edit {target_dir / 'intents' / f'{name}.yaml'} to set the real image, env, ports, and volume size.")
    info("  2. Register the partition in stratatools image/release configuration before using st-image or st-release.")
    info(f"  3. Roll it out with: st-release --partition {name}")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    parent_dir: Path = typer.Option(
        ROOT.parent, "--parent-dir",
        help="Directory in which sibling repos live (default: ainfra/).",
    ),
    check_only: bool = typer.Option(
        False, "--check-only", help="Skip cloning; only run prerequisite checks.",
    ),
    repo: list[str] = typer.Option(
        None, "--repo", help="Repo name (repeatable). Default: all.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print clone commands without executing.",
    ),
    auto_kind: bool = typer.Option(
        True,
        "--auto-kind/--no-auto-kind",
        help="Create or reuse a local kind cluster automatically when no Kubernetes cluster is reachable.",
    ),
    kind_name: str = typer.Option(
        DEFAULT_KIND_CLUSTER,
        "--kind-name",
        help="Name of the kind cluster to create or reuse when auto-kind is enabled.",
    ),
    kind_workers: int = typer.Option(
        DEFAULT_KIND_WORKERS,
        "--kind-workers",
        min=1,
        help="Number of worker nodes for the auto-created kind cluster.",
    ),
    no_install_hints: bool = typer.Option(
        False, "--no-install-hints", help="Suppress per-tool install instructions.",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    names = list(repo or REPOS.keys())
    unknown = [n for n in names if n not in REPOS]
    if unknown:
        raise typer.BadParameter(f"unknown repo(s): {', '.join(unknown)}")

    pkey = _platform_key()

    info("=== Required tools ===")
    missing: list[str] = []
    missing_optional: list[str] = []
    cluster_ok = False

    def _check(name: str, cmd: list[str], *, required: bool = True, version_ok=None) -> None:
        ok, ver = _try_version(cmd)
        vok = ok and (version_ok(ver) if version_ok else True)
        detail = ver or ("(missing)" if required else "(optional, missing)")
        if ok and not vok:
            detail = ver + " (version too old)"
        info(_row(_mark("ok" if vok else ("fail" if required else "opt")), name, detail))
        if not vok:
            (missing if required else missing_optional).append(name)

    _check("git", ["git", "--version"])
    _check("docker", ["docker", "--version"])
    if "docker" not in missing:
        d_ok, _ = _try_version(["docker", "info"])
        info(_row(_mark("ok" if d_ok else "fail"), "docker daemon", "running" if d_ok else "not running"))
        if not d_ok:
            missing.append("docker")
    _check("kubectl", ["kubectl", "version", "--client"])
    py_ok = sys.version_info >= (3, 11)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    info(_row(_mark("ok" if py_ok else "fail"), "python3", py_ver + ("" if py_ok else " (need >=3.11)")))
    if not py_ok:
        missing.append("python3")
    _check("go", ["go", "version"], version_ok=lambda v: (lambda gv: gv is not None and gv >= (1, 22))(_parse_go_version(v)))
    _check("make", ["make", "--version"])
    _check("uv", ["uv", "--version"], required=False)
    guardianctl_path = shutil.which("guardianctl")
    if guardianctl_path:
        ok, _ = _try_version(["guardianctl", "--help"])
        detail = guardianctl_path if ok else f"{guardianctl_path} (present, help probe failed)"
        info(_row(_mark("ok"), "guardianctl", detail))
    else:
        info(_row(_mark("opt"), "guardianctl", "(optional, missing)"))
        missing_optional.append("guardianctl")

    info("")
    info("=== Kubernetes cluster ===")
    cluster_ok = _check_cluster_reachability(
        missing,
        auto_kind=auto_kind,
        kind_name=kind_name,
        kind_workers=kind_workers,
        dry_run=dry_run,
        allow_create=not check_only,
    )

    # Repos
    info("")
    info("=== Repositories ===")
    if check_only:
        for name in names:
            dest = parent_dir / name
            if dest.exists():
                info(_row(_mark("ok"), name, str(dest)))
            else:
                info(_row(_mark("fail"), name, "(will clone)"))
    else:
        info(f"(parent: {parent_dir})")
        clone_results = _clone_all(parent_dir, names, dry_run)
        for name, ok, dest in clone_results:
            info(_row(_mark("ok" if ok else "fail"), name, str(dest)))

        monofs_dir = parent_dir / "monofs"
        if monofs_dir.is_dir():
            if dry_run:
                info(_row(_mark("opt"), "monofs key", f"would ensure {monofs_dir / '.env'}"))
            else:
                try:
                    key_state = ensure_monofs_encryption_key(repo_dir=monofs_dir)
                except ValueError as exc:
                    raise typer.Exit(code=1) from typer.BadParameter(str(exc))
                detail = f"{key_state.env_file} ({'created' if key_state.created else 'reused'})"
                info(_row(_mark("ok"), "monofs key", detail))

    # Install hints
    show_hints = (not no_install_hints) and (missing or not cluster_ok or missing_optional)
    if show_hints:
        info("")
        info("=== Install instructions ===")
        for tool in missing + missing_optional:
            hints = _hints_for(tool, pkey)
            info(f"{tool} is missing. Install with:")
            for line in hints:
                info(f"  {line}" if not line.startswith(" ") else line)
            info("")
        if not cluster_ok and "kubectl" not in missing:
            for line in CLUSTER_HINT:
                info(line)
            info("")

    # Final status line + exit
    n_missing = len(missing)
    pieces = []
    if n_missing:
        pieces.append(f"{n_missing} required tool{'s' if n_missing != 1 else ''} missing")
    if not cluster_ok:
        pieces.append("cluster unreachable")

    if n_missing or not cluster_ok:
        info(f"Exit code 1: {', '.join(pieces)}.")
        raise typer.Exit(code=1)
    info("All required tools present, cluster reachable.")
