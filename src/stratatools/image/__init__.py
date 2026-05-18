"""st-image — build, push/load, and stamp partition images.

Radically simplified port of ``ainfra/scripts/image``. Always stamps to
``<registry>/<repo>:latest`` (or, in cluster-load mode, ``<repo>:latest``).
"""
from __future__ import annotations

import os
import subprocess
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

PARTITIONS_LIST: list[str] = [
    "guardian-configs", "opentelemetry", "k8s-top",
    "doctor", "monitoring", "dev-workspace",
]

# Each recipe: (image_tag, extra_docker_build_args, build_context_dir)
BUILD_RECIPES: dict[str, list[tuple[str, list[str], Path]]] = {
    "guardian-configs": [
        ("guardian:latest",
         ["--build-context", f"monofs={MONOFS_REPO_DIR}",
          "--build-context", f"kvs={KVS_REPO_DIR}"],
         GUARDIAN_REPO_DIR),
        ("guardian-pusher-k8s:latest",
         ["-f", str(GUARDIAN_REPO_DIR / "Dockerfile.pusher-k8s"),
          "--build-context", f"monofs={MONOFS_REPO_DIR}",
          "--build-context", f"kvs={KVS_REPO_DIR}"],
         GUARDIAN_REPO_DIR),
    ],
    "opentelemetry": [],
    "k8s-top": [
        ("k8s-top:latest", ["-f", str(AINFRA / "k8s-top" / "Dockerfile")], AINFRA),
    ],
    "doctor": [
        ("doctor-ingest:latest",
         ["--target", "ingest", "--build-context", f"monofs={MONOFS_REPO_DIR}"],
         DOCTOR_REPO_DIR),
        ("doctor-query:latest",
         ["--target", "query", "--build-context", f"monofs={MONOFS_REPO_DIR}"],
         DOCTOR_REPO_DIR),
    ],
    "monitoring": [],
    "dev-workspace": [
        ("monofs-client:dev-base", ["--target", "client"], MONOFS_REPO_DIR),
        ("dev-workspace-vscode:latest",
         ["-f", str(ROOT / "images" / "dev-workspace-vscode" / "Dockerfile")],
         AINFRA),
    ],
}

OTEL_UPSTREAM = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.108.0"
HAPROXY_UPSTREAM = "mirror.gcr.io/library/haproxy:2.9"
GRAFANA_UPSTREAM = "mirror.gcr.io/grafana/grafana:13.0.0"

# (upstream_ref, local_repo:tag-without-registry)
MIRROR_RECIPES: dict[str, list[tuple[str, str]]] = {
    "opentelemetry": [
        (OTEL_UPSTREAM, "otel/opentelemetry-collector-contrib:latest"),
        (HAPROXY_UPSTREAM, "library/haproxy:2.9"),
    ],
    "monitoring": [
        (GRAFANA_UPSTREAM, "grafana/grafana:13.0.0"),
        (HAPROXY_UPSTREAM, "library/haproxy:2.9"),
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


def _stamp_file(path: Path, mapping: dict[str, str], dry_run: bool) -> bool:
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as e:
        warn(f"skip {path}: {e}")
        return False
    if data is None:
        return False
    changed = False
    repo_map = _build_repo_mapping(mapping)

    def walk(node):
        nonlocal changed
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "image" and isinstance(v, str):
                    if v in mapping and mapping[v] != v:
                        # exact key match and value differs → replace
                        node[k] = mapping[v]
                        changed = True
                    elif v not in mapping:
                        # no exact match — try repo-name based fallback
                        repo = _image_repo_name(v)
                        if repo in repo_map and repo_map[repo] != v:
                            node[k] = repo_map[repo]
                            changed = True
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not changed:
        return False
    info(f"  stamp {path.relative_to(ROOT)}")
    if not dry_run:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return True


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
            run(["docker", "build", "-t", tag, *extra, str(ctx)], dry_run=dry_run)


def cmd_push(partitions: list[str], registry: str, dry_run: bool) -> None:
    ctx = kubectl_context()
    cluster_load = _cluster_load_mode(ctx)
    info(f"kubectl context: {ctx or '<none>'} "
         f"({'cluster-load' if cluster_load else 'registry push'})")
    for part in partitions:
        info(f"[{part}] {'load' if cluster_load else 'push'}")
        for tag, _e, _c in BUILD_RECIPES.get(part, []):
            if cluster_load:
                _cluster_load(tag, dry_run)
            else:
                remote = f"{registry}/{tag}"
                run(["docker", "tag", tag, remote], dry_run=dry_run)
                run(["docker", "push", remote], dry_run=dry_run)
        for upstream, local in MIRROR_RECIPES.get(part, []):
            run(["docker", "pull", upstream], dry_run=dry_run)
            if cluster_load:
                local_tag = local.split(":", 1)[0] + ":latest"
                run(["docker", "tag", upstream, local_tag], dry_run=dry_run)
                _cluster_load(local_tag, dry_run)
            else:
                remote = f"{registry}/{local}"
                run(["docker", "tag", upstream, remote], dry_run=dry_run)
                run(["docker", "push", remote], dry_run=dry_run)


def cmd_stamp(partitions: list[str], registry: str, dry_run: bool) -> None:
    cluster_load = _cluster_load_mode(kubectl_context())
    for part in partitions:
        info(f"[{part}] stamp")
        mapping: dict[str, str] = {}
        for tag, _e, _c in BUILD_RECIPES.get(part, []):
            repo = tag.split(":", 1)[0]
            mapping[tag] = tag if cluster_load else f"{registry}/{repo}:latest"
        for upstream, local in MIRROR_RECIPES.get(part, []):
            repo = local.split(":", 1)[0]
            mapping[upstream] = f"{repo}:latest" if cluster_load else f"{registry}/{repo}:latest"
        if not mapping:
            info("  (no images to stamp)")
            continue
        part_dir = PARTITIONS / part
        targets: list[Path] = []
        for sub in ("intents", "payloads"):
            d = part_dir / sub
            if d.is_dir():
                targets.extend(sorted(d.glob("*.yaml")))
        if not targets:
            info(f"  (no yaml files under {part_dir})")
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
