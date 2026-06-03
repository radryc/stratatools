"""st-image — build, push/load, and stamp partition images.

Radically simplified port of ``ainfra/scripts/image``. Stamps immutable image
refs derived from the local image content instead of mutable ``:latest`` tags.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import typer
import yaml

from stratatools.util import PARTITIONS, ROOT, die, info, kubectl_context, run, warn

# Repo layout -----------------------------------------------------------------
AINFRA = ROOT.parent
GUARDIAN_REPO_DIR = Path(os.environ.get("GUARDIAN_REPO_DIR", AINFRA / "guardian"))
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", AINFRA / "monofs"))
DOCTOR_REPO_DIR = Path(os.environ.get("DOCTOR_REPO_DIR", AINFRA / "doctor"))
KVS_REPO_DIR = Path(os.environ.get("KVS_REPO_DIR", AINFRA / "kvs"))
K8S_TOP_REPO_DIR = Path(os.environ.get("K8S_TOP_REPO_DIR", AINFRA / "k8s-top"))
AGENT_REPO_DIR = Path(os.environ.get("AGENT_REPO_DIR", AINFRA / "agent"))
LB_REPO_DIR = Path(os.environ.get("LB_REPO_DIR", AINFRA / "lb"))

PARTITIONS_LIST: list[str] = [
    "guardian-configs", "opentelemetry", "k8s-top",
    "doctor", "monitoring", "dev-workspace", "agent", "lb-agent",
]

# Each recipe: (image_tag, extra_docker_build_args, build_context_dir)
BUILD_RECIPES: dict[str, list[tuple[str, list[str], Path]]] = {
    "guardian-configs": [
        ("guardian:latest",
         ["--build-context", f"monofs={MONOFS_REPO_DIR}",
          "--build-context", f"kvs={KVS_REPO_DIR}"],
         GUARDIAN_REPO_DIR),
        ("lb:latest", [], LB_REPO_DIR),
        ("guardian-pusher-k8s:latest",
         ["-f", str(GUARDIAN_REPO_DIR / "Dockerfile.pusher-k8s"),
          "--build-context", f"monofs={MONOFS_REPO_DIR}",
          "--build-context", f"kvs={KVS_REPO_DIR}"],
         GUARDIAN_REPO_DIR),
        ("guardian-pusher-aws:latest",
         ["-f", str(GUARDIAN_REPO_DIR / "Dockerfile.pusher-aws"),
          "--build-context", f"monofs={MONOFS_REPO_DIR}",
          "--build-context", f"kvs={KVS_REPO_DIR}"],
         GUARDIAN_REPO_DIR),
    ],
    "opentelemetry": [
        ("lb:latest", [], LB_REPO_DIR),
    ],
    "k8s-top": [
        ("k8s-top:latest", ["-f", str(K8S_TOP_REPO_DIR / "Dockerfile")], AINFRA),
    ],
    "doctor": [
        ("lb:latest", [], LB_REPO_DIR),
        ("doctor-ingest:latest",
         ["--build-arg", "DOCTOR_SERVICE=doctor-ingest", "--build-context", f"monofs={MONOFS_REPO_DIR}"],
         DOCTOR_REPO_DIR),
        ("doctor-query:latest",
         ["--build-arg", "DOCTOR_SERVICE=doctor-query", "--build-context", f"monofs={MONOFS_REPO_DIR}"],
         DOCTOR_REPO_DIR),
    ],
    "monitoring": [
        ("lb:latest", [], LB_REPO_DIR),
    ],
    "lb-agent": [
        ("lb:latest", [], LB_REPO_DIR),
    ],
    "dev-workspace": [
        ("lb:latest", [], LB_REPO_DIR),
        ("monofs-client:dev-base", ["--target", "client"], MONOFS_REPO_DIR),
        ("dev-workspace-vscode:latest",
         ["-f", str(ROOT / "images" / "dev-workspace-vscode" / "Dockerfile"),
          "--build-arg", "BASE_IMAGE=monofs-client:dev-base"],
         AINFRA),
    ],
    "agent": [
        ("lb:latest", [], LB_REPO_DIR),
        ("lagent-llm:latest", [], AGENT_REPO_DIR / "llm"),
        ("lagent-backend:latest", [], AGENT_REPO_DIR / "backend"),
        ("lagent-frontend:latest", [], AGENT_REPO_DIR / "frontend"),
    ],
}

OTEL_UPSTREAM = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.108.0"
GRAFANA_UPSTREAM = "mirror.gcr.io/grafana/grafana:13.0.0"

# (upstream_ref, local_repo:tag-without-registry)
MIRROR_RECIPES: dict[str, list[tuple[str, str]]] = {
    "opentelemetry": [
        (OTEL_UPSTREAM, "otel/opentelemetry-collector-contrib:latest"),
    ],
    "monitoring": [
        (GRAFANA_UPSTREAM, "grafana/grafana:13.0.0"),
    ],
}


# Helpers ---------------------------------------------------------------------
def _resolve(partitions: Iterable[str] | None) -> list[str]:
    parts = list(partitions or [])
    if not parts:
        return list(PARTITIONS_LIST)
    bad = [p for p in parts if p not in PARTITIONS_LIST]
    if bad:
        die(f"unknown partition(s): {', '.join(bad)}")
    return parts


def _cluster_load_mode(ctx: str) -> bool:
    return ctx == "docker-desktop" or ctx.startswith("kind-")


def _git_output(repo: Path, args: list[str], default: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return default
    value = result.stdout.strip()
    return value or default


def _monofs_build_args() -> list[str]:
    version = os.environ.get("MONOFS_BUILD_VERSION") or os.environ.get("BUILD_VERSION") or "dev"
    commit = (
        os.environ.get("MONOFS_BUILD_COMMIT")
        or os.environ.get("BUILD_COMMIT")
        or _git_output(MONOFS_REPO_DIR, ["rev-parse", "--short=12", "HEAD"], "unknown")
    )
    dirty = _git_output(MONOFS_REPO_DIR, ["status", "--porcelain"], "")
    if dirty and not commit.endswith("-dirty"):
        commit = f"{commit}-dirty"
    build_time = (
        os.environ.get("MONOFS_BUILD_TIME")
        or os.environ.get("BUILD_TIME")
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    return [
        "--build-arg", f"VERSION={version}",
        "--build-arg", f"COMMIT={commit}",
        "--build-arg", f"BUILD_TIME={build_time}",
    ]


def _image_repo_name(ref: str) -> str:
    """Extract the bare image name from a ref, stripping registry prefix and tag/digest.

    Examples:
      "localhost:5000/guardian:sha256-abc123" -> "guardian"
      "guardian:latest"                        -> "guardian"
      "ghcr.io/org/my-image:1.2.3"             -> "my-image"
      "mirror.gcr.io/grafana/grafana:13.0.0"   -> "grafana/grafana"  (multi-component)
    """
    # strip digest
    ref = ref.split("@")[0]
    # strip tag
    last_slash = ref.rfind("/")
    name_and_tag = ref if last_slash == -1 else ref[last_slash + 1:]
    name = name_and_tag.split(":")[0]
    return name


def is_immutable_image_ref(ref: str) -> bool:
    if "@sha256:" in ref:
        return True
    last_slash = ref.rfind("/")
    name_and_tag = ref if last_slash == -1 else ref[last_slash + 1:]
    if ":" not in name_and_tag:
        return False
    return name_and_tag.split(":", 1)[1].startswith("sha256-")


def _image_id_suffix(image: str, dry_run: bool) -> str:
    result = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture=True,
        check=False,
    )
    if result and result.returncode == 0:
        image_id = result.stdout.strip()
        if image_id.startswith("sha256:"):
            image_id = image_id.split(":", 1)[1]
        if image_id:
            return f"sha256-{image_id[:16]}"
    if dry_run:
        return "sha256-dry-run"
    detail = ""
    if result is not None:
        detail = (result.stderr or result.stdout).strip()
    die(f"failed to inspect docker image {image}: {detail or 'image not present locally'}")


def _immutable_image_ref(source_ref: str, repo: str, registry: str, cluster_load: bool, dry_run: bool) -> str:
    suffix = _image_id_suffix(source_ref, dry_run)
    if cluster_load:
        return f"{repo}:{suffix}"
    return f"{registry}/{repo}:{suffix}"


def _partition_targets(part: str) -> list[Path]:
    part_dir = PARTITIONS / part
    targets: list[Path] = []
    for sub in ("intents", "payloads"):
        d = part_dir / sub
        if d.is_dir():
            targets.extend(sorted(d.glob("*.yaml")))
    return targets


def _partition_mapping(part: str, registry: str, cluster_load: bool, dry_run: bool) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for tag, _e, _c in BUILD_RECIPES.get(part, []):
        repo = tag.split(":", 1)[0]
        mapping[tag] = _immutable_image_ref(tag, repo, registry, cluster_load, dry_run)
    for upstream, local in MIRROR_RECIPES.get(part, []):
        repo = local.split(":", 1)[0]
        mapping[upstream] = _immutable_image_ref(upstream, repo, registry, cluster_load, dry_run)
    return mapping


def _build_repo_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Build a fallback mapping from bare repo name → target ref.

    Used when an image field in a YAML contains an old registry+sha ref that
    doesn't appear verbatim as a mapping key (e.g. a previously registry-stamped
    sha256 ref that must be rewritten for cluster-load mode, or vice-versa).
    """
    repo_map: dict[str, str] = {}
    for target in mapping.values():
        repo = _image_repo_name(target)
        # prefer the first (most specific) target for each repo name
        if repo not in repo_map:
            repo_map[repo] = target
    return repo_map


def _stamp_file(path: Path, mapping: dict[str, str], dry_run: bool, *, announce: bool = True) -> list[tuple[str, str]]:
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as e:
        warn(f"skip {path}: {e}")
        return []
    if data is None:
        return []
    repo_map = _build_repo_mapping(mapping)
    changes: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "image" and isinstance(v, str):
                    replacement = None
                    if v in mapping and mapping[v] != v:
                        replacement = mapping[v]
                    elif v not in mapping:
                        repo = _image_repo_name(v)
                        if repo in repo_map and repo_map[repo] != v:
                            replacement = repo_map[repo]
                    if replacement is not None:
                        changes.append((v, replacement))
                        if not dry_run:
                            node[k] = replacement
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not changes:
        return []
    if announce:
        info(f"  stamp {path.relative_to(ROOT)}")
    if not dry_run:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return changes


def planned_stamp_changes(partitions: list[str], registry: str, dry_run: bool) -> dict[str, list[tuple[Path, list[tuple[str, str]]]]]:
    cluster_load = _cluster_load_mode(kubectl_context())
    out: dict[str, list[tuple[Path, list[tuple[str, str]]]]] = {}
    for part in partitions:
        mapping = _partition_mapping(part, registry, cluster_load, dry_run)
        if not mapping:
            continue
        file_changes: list[tuple[Path, list[tuple[str, str]]]] = []
        for target in _partition_targets(part):
            changes = _stamp_file(target, mapping, True, announce=False)
            if changes:
                file_changes.append((target, changes))
        if file_changes:
            out[part] = file_changes
    return out


def _cluster_load(image: str, dry_run: bool) -> None:
    r = run(["kubectl", "get", "nodes", "-o", "name"], capture=True, check=False, dry_run=dry_run)
    if dry_run:
        info(f"  (dry-run) would cluster-load {image} into all nodes")
        return
    if not r or r.returncode != 0:
        die("failed to list cluster nodes via kubectl")
    nodes = [n.split("/", 1)[1] for n in r.stdout.splitlines() if n.strip()]
    if not nodes:
        die("no cluster nodes found")
    for node in nodes:
        # Immutable refs can be safely skipped when already present on a node.
        present = run(
            ["docker", "exec", node, "ctr", "-n=k8s.io", "images", "inspect", image],
            capture=True,
            check=False,
        )
        if present and present.returncode == 0:
            info(f"  skip {image} (already present) → {node}")
            continue
        info(f"  load {image} → {node}")
        save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
        importer = subprocess.Popen(
            ["docker", "exec", "-i", node, "ctr", "-n=k8s.io", "images", "import", "-"],
            stdin=save.stdout,
        )
        if save.stdout is not None:
            save.stdout.close()
        importer.communicate()
        save.wait()
        if importer.returncode != 0 or save.returncode != 0:
            die(f"cluster-load failed for {image} on {node}")


# Commands --------------------------------------------------------------------
app = typer.Typer(no_args_is_help=False, help="build/push/stamp partition images")


@app.command("list")
def cmd_list() -> None:
    """List known partitions."""
    for p in PARTITIONS_LIST:
        info(p)


def cmd_build(partitions: list[str], dry_run: bool) -> None:
    for part in partitions:
        recipes = BUILD_RECIPES.get(part, [])
        if not recipes:
            info(f"[{part}] no local build step")
            continue
        info(f"[{part}] build")
        for tag, extra, ctx in recipes:
            build_args = _monofs_build_args() if ctx == MONOFS_REPO_DIR else []
            run(["docker", "build", "-t", tag, *extra, *build_args, str(ctx)], dry_run=dry_run)


def cmd_push(partitions: list[str], registry: str, dry_run: bool) -> None:
    ctx = kubectl_context()
    cluster_load = _cluster_load_mode(ctx)
    info(f"kubectl context: {ctx or '<none>'} "
         f"({'cluster-load' if cluster_load else 'registry push'})")
    for part in partitions:
        info(f"[{part}] {'load' if cluster_load else 'push'}")
        # Pull mirror images before _partition_mapping so the inspect can succeed.
        for upstream, _local in MIRROR_RECIPES.get(part, []):
            run(["docker", "pull", upstream], dry_run=dry_run)
        mapping = _partition_mapping(part, registry, cluster_load, dry_run)
        for tag, _e, _c in BUILD_RECIPES.get(part, []):
            target = mapping[tag]
            run(["docker", "tag", tag, target], dry_run=dry_run)
            if cluster_load:
                _cluster_load(target, dry_run)
            else:
                run(["docker", "push", target], dry_run=dry_run)
        for upstream, local in MIRROR_RECIPES.get(part, []):
            target = mapping[upstream]
            run(["docker", "tag", upstream, target], dry_run=dry_run)
            if cluster_load:
                _cluster_load(target, dry_run)
            else:
                run(["docker", "push", target], dry_run=dry_run)


def cmd_stamp(partitions: list[str], registry: str, dry_run: bool) -> None:
    cluster_load = _cluster_load_mode(kubectl_context())
    for part in partitions:
        info(f"[{part}] stamp")
        mapping = _partition_mapping(part, registry, cluster_load, dry_run)
        if not mapping:
            info("  (no images to stamp)")
            continue
        targets = _partition_targets(part)
        if not targets:
            info(f"  (no yaml files under {PARTITIONS / part})")
            continue
        for f in targets:
            _stamp_file(f, mapping, dry_run)


def cmd_all(partitions: list[str], registry: str, dry_run: bool) -> None:
    cmd_build(partitions, dry_run)
    cmd_push(partitions, registry, dry_run)
    cmd_stamp(partitions, registry, dry_run)


# Typer option singletons
_P = typer.Option(None, "--partition", "-p", help="Partition (repeatable). Default: all.")
_R = typer.Option("localhost:5000", "--registry", "-r", help="Registry host:port.")
_D = typer.Option(False, "--dry-run", help="Print commands only.")


@app.command("build")
def _build(partition: list[str] = _P, dry_run: bool = _D) -> None:
    cmd_build(_resolve(partition), dry_run)


@app.command("push")
def _push(partition: list[str] = _P, registry: str = _R, dry_run: bool = _D) -> None:
    cmd_push(_resolve(partition), registry, dry_run)


@app.command("stamp")
def _stamp(partition: list[str] = _P, registry: str = _R, dry_run: bool = _D) -> None:
    cmd_stamp(_resolve(partition), registry, dry_run)


@app.command("all")
def _all(partition: list[str] = _P, registry: str = _R, dry_run: bool = _D) -> None:
    cmd_all(_resolve(partition), registry, dry_run)


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    partition: list[str] = _P,
    registry: str = _R,
    dry_run: bool = _D,
) -> None:
    """Default action: run ``all`` when no subcommand given."""
    if ctx.invoked_subcommand is None:
        cmd_all(_resolve(partition), registry, dry_run)
