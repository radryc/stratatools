"""st-setup — clone sibling repos and run prerequisite checks."""
from __future__ import annotations

from datetime import datetime
import os
import platform
import re
import shlex
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
    "devdns":   "https://github.com/radryc/devdns.git",
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

KIND_CLUSTER_NAME = "stratatools"
KIND_CLUSTER_CONFIG = """\
apiVersion: kind.x-k8s.io/v1alpha4
kind: Cluster
nodes:
- role: control-plane
- role: worker
- role: worker
- role: worker
"""

app = typer.Typer(no_args_is_help=False, help="bootstrap sibling repos and verify host prerequisites")


# ── Auto-install helpers (Linux only) ────────────────────────────────────────


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _is_auto_install_supported(pkey: str) -> bool:
    """kubectl and kind can be auto-installed on native Linux and WSL."""
    return pkey.startswith("linux") or pkey == "wsl"


def _docker_auto_install_supported(pkey: str) -> bool:
    """Docker auto-install only on native Linux (WSL uses Docker Desktop on the Windows host)."""
    return pkey.startswith("linux")


def _run_cmd(cmd: list[str], *, timeout: int = 60) -> bool:
    """Run a command, stream output to the terminal, return True on success."""
    try:
        r = subprocess.run(cmd, timeout=timeout)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _auto_install_docker() -> tuple[bool, bool]:
    """Ensure docker is installed and running.

    Returns (success, fresh_install).
    """
    if shutil.which("docker"):
        # Binary present but daemon not running — try to start the service.
        info("  → Docker binary found; starting daemon via systemctl…")
        if shutil.which("systemctl"):
            _run_cmd(["sudo", "systemctl", "enable", "--now", "docker"], timeout=30)
        ok, _ = _try_version(["docker", "info"])
        if ok:
            return True, False
        info("  ✗ Could not start Docker daemon.")
        return False, False

    # Docker not installed — install via official get-docker.sh script.
    info("  → Downloading Docker install script (requires sudo)…")
    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as tf:
        script_path = tf.name
    try:
        if not _run_cmd(["curl", "-fsSL", "https://get.docker.com", "-o", script_path]):
            info("  ✗ Failed to download Docker install script.")
            return False, False
        info("  → Running Docker install script (this may take a few minutes)…")
        if not _run_cmd(["sudo", "sh", script_path], timeout=300):
            info("  ✗ Docker install script failed.")
            return False, False
    finally:
        Path(script_path).unlink(missing_ok=True)

    # Add current user to the docker group.
    user = os.environ.get("USER") or os.environ.get("LOGNAME", "")
    if user:
        r = subprocess.run(["sudo", "usermod", "-aG", "docker", user], check=False)
        if r.returncode != 0:
            info("  ⚠ Could not add user to docker group.")

    # Enable and start the service.
    if shutil.which("systemctl"):
        _run_cmd(["sudo", "systemctl", "enable", "--now", "docker"], timeout=30)

    # Verify via sudo since the current session lacks the docker group.
    r = subprocess.run(["sudo", "docker", "info"], capture_output=True, timeout=15)
    if r.returncode != 0:
        info("  ✗ Docker daemon not responding after install.")
        return False, False

    info("  → Docker installed. Note: log out/in for group permissions to take effect.")
    return True, True


def _auto_install_kubectl() -> bool:
    """Download and install the latest stable kubectl binary."""
    info("  → Fetching latest kubectl version…")
    try:
        r = subprocess.run(
            ["curl", "-fsSL", "https://dl.k8s.io/release/stable.txt"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        version = r.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        info("  ✗ Could not fetch kubectl version.")
        return False

    arch = _arch()
    url = f"https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl"
    info(f"  → Downloading kubectl {version} ({arch})…")
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tmp_path = tf.name
    try:
        if not _run_cmd(["curl", "-fsSLo", tmp_path, url]):
            info("  ✗ Failed to download kubectl.")
            return False
        if not _run_cmd(["sudo", "install", "-m", "0755", tmp_path, "/usr/local/bin/kubectl"]):
            info("  ✗ Failed to install kubectl.")
            return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return True


def _auto_install_kind() -> bool:
    """Download and install the latest kind binary."""
    arch = _arch()
    url = f"https://kind.sigs.k8s.io/dl/latest/kind-linux-{arch}"
    info(f"  → Downloading kind ({arch})…")
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tmp_path = tf.name
    try:
        if not _run_cmd(["curl", "-fsSLo", tmp_path, url]):
            info("  ✗ Failed to download kind.")
            return False
        Path(tmp_path).chmod(0o755)
        if not _run_cmd(["sudo", "mv", tmp_path, "/usr/local/bin/kind"]):
            info("  ✗ Failed to install kind.")
            return False
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        return False
    return True


def _create_kind_cluster(*, docker_fresh: bool) -> bool:
    """Create (or verify) a 4-node kind cluster — 1 control-plane + 3 workers."""
    # If cluster is already registered in kind, just refresh the kubeconfig.
    r = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True, check=False)
    if r.returncode == 0 and KIND_CLUSTER_NAME in r.stdout.split():
        info(f"  → Cluster '{KIND_CLUSTER_NAME}' already registered; refreshing kubeconfig…")
        subprocess.run(["kind", "export", "kubeconfig", "--name", KIND_CLUSTER_NAME], check=False)
        c_ok, _ = _try_version(["kubectl", "cluster-info", "--context", f"kind-{KIND_CLUSTER_NAME}"])
        if c_ok:
            return True
        info(f"  → Existing cluster unreachable; recreating…")
        subprocess.run(["kind", "delete", "cluster", "--name", KIND_CLUSTER_NAME], check=False)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
        tf.write(KIND_CLUSTER_CONFIG)
        config_path = tf.name

    base_cmd = [
        "kind", "create", "cluster",
        "--name", KIND_CLUSTER_NAME,
        "--config", config_path,
    ]
    try:
        info(f"  → Creating 4-node cluster '{KIND_CLUSTER_NAME}' (1 control-plane + 3 workers)…")
        if docker_fresh:
            # Docker group isn't active in this session yet.
            # Use 'sg docker' to re-enter the group within the same session.
            if shutil.which("sg"):
                cmd_str = " ".join(shlex.quote(c) for c in base_cmd)
                result = subprocess.run(["sg", "docker", "-c", cmd_str], timeout=600)
            else:
                result = subprocess.run(base_cmd, timeout=600)
            if result.returncode != 0:
                info("  ⚠ Cluster creation failed — docker group may not be active yet.")
                info("    Log out, log back in, then re-run `st-setup`.")
                return False
        else:
            if not _run_cmd(base_cmd, timeout=600):
                info("  ✗ kind create cluster failed.")
                return False

        # Refresh kubeconfig and verify reachability.
        subprocess.run(["kind", "export", "kubeconfig", "--name", KIND_CLUSTER_NAME], check=False)
        c_ok, _ = _try_version(["kubectl", "cluster-info", "--context", f"kind-{KIND_CLUSTER_NAME}"])
        if not c_ok:
            info(f"  ✗ Cluster created but kubectl cannot reach it (context kind-{KIND_CLUSTER_NAME}).")
            return False
        return True
    except subprocess.TimeoutExpired:
        info("  ✗ Cluster creation timed out.")
        return False
    finally:
        Path(config_path).unlink(missing_ok=True)


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
    no_install_hints: bool = typer.Option(
        False, "--no-install-hints", help="Suppress per-tool install instructions.",
    ),
    auto_install: bool = typer.Option(
        True, "--auto-install/--no-auto-install",
        help="Automatically install missing Docker/Kubernetes tools on Linux (default: on).",
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
    if "kubectl" not in missing:
        c_ok, _ = _try_version(["kubectl", "cluster-info"])
        cluster_ok = c_ok
        info(_row(_mark("ok" if c_ok else "fail"), "cluster", "reachable" if c_ok else "unreachable"))
    else:
        info(_row(_mark("fail"), "cluster", "kubectl missing"))

    # ── Auto-install missing Docker / Kubernetes on native Linux ─────────────
    if auto_install and _is_auto_install_supported(pkey):
        docker_fresh = False

        if "docker" in missing and _docker_auto_install_supported(pkey):
            info("")
            info("=== Auto-installing Docker ===")
            ok, docker_fresh = _auto_install_docker()
            if ok:
                missing.remove("docker")
                label = "installed" if docker_fresh else "daemon started"
                info(_row(_mark("ok"), "docker", label))
            else:
                info(_row(_mark("fail"), "docker", "auto-install failed — see hints below"))

        if "kubectl" in missing:
            info("")
            info("=== Auto-installing kubectl ===")
            if _auto_install_kubectl():
                missing.remove("kubectl")
                info(_row(_mark("ok"), "kubectl", "installed"))
            else:
                info(_row(_mark("fail"), "kubectl", "auto-install failed — see hints below"))

        if not cluster_ok and "kubectl" not in missing:
            info("")
            info("=== Setting up Kubernetes cluster (kind) ===")
            kind_installed = bool(shutil.which("kind"))
            if not kind_installed:
                info("  kind not found — installing…")
                kind_installed = _auto_install_kind()
                if not kind_installed:
                    info(_row(_mark("fail"), "kind", "install failed — see hints below"))

            if kind_installed:
                if _create_kind_cluster(docker_fresh=docker_fresh):
                    cluster_ok = True
                    info(_row(_mark("ok"), "cluster", f"kind/{KIND_CLUSTER_NAME} ready (4 nodes)"))

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
