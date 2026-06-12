"""st-image — build, push/load, and stamp partition images.

Radically simplified port of ``ainfra/scripts/image``. Stamps immutable image
refs derived from the local image content instead of mutable ``:latest`` tags.
"""
from __future__ import annotations

import concurrent.futures
import functools
import json
import os
import shutil
import subprocess
import tempfile
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
LOLIPOP_REPO_DIR = Path(
    os.environ.get("LOLIPOP_REPO_DIR", Path.home() / "aiprojects" / "lolipop")
)

PARTITIONS_LIST: list[str] = [
    "guardian-configs", "opentelemetry", "k8s-top",
    "doctor", "monitoring", "dev-workspace", "agent", "lb-agent", "lolipop",
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
        ("guardian-pusher-docker:latest",
         ["-f", str(GUARDIAN_REPO_DIR / "Dockerfile.pusher-docker"),
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
    "lolipop": [
        ("lolipop-frontend:latest", [], LOLIPOP_REPO_DIR / "frontend"),
        ("lolipop-backend:latest", [], LOLIPOP_REPO_DIR / "backend"),
        ("lolipop-qwen-tts:latest", [], LOLIPOP_REPO_DIR / "qwen-tts-service"),
        ("lolipop-lora-trainer:latest", [], LOLIPOP_REPO_DIR / "lora-trainer"),
        ("lolipop-wangp:latest", [], LOLIPOP_REPO_DIR / "wan2gp-docker"),
    ],
}

# Each prepare recipe: (git_repo_root, build_context_dir, staged_dest_dir)
IMAGEBUILD_PREPARE_RECIPES: dict[str, list[tuple[Path, Path, Path]]] = {
    "agent": [
        (
            AGENT_REPO_DIR,
            AGENT_REPO_DIR / "llm",
            PARTITIONS / "agent" / "payloads" / "sources" / "lagent-llm",
        ),
        (
            AGENT_REPO_DIR,
            AGENT_REPO_DIR / "backend",
            PARTITIONS / "agent" / "payloads" / "sources" / "lagent-backend",
        ),
        (
            AGENT_REPO_DIR,
            AGENT_REPO_DIR / "frontend",
            PARTITIONS / "agent" / "payloads" / "sources" / "lagent-frontend",
        ),
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


def _kind_cluster_name(ctx: str) -> str | None:
    """Return the kind cluster name from a kubectl context name, or None."""
    if ctx.startswith("kind-"):
        return ctx[len("kind-"):]
    return None


def kind_load_images(images: list[str], dry_run: bool) -> None:
    """Load images into a kind cluster using `kind load docker-image`.

    This correctly repoints the :latest tag on all nodes to the current local
    digest, unlike the custom ctr-import path which leaves stale tag pointers.

    After loading, any pods using the updated images are force-deleted so that
    when Kubernetes reschedules them they pick up the newly tagged digest rather
    than whatever image ID the kubelet had previously cached.
    """
    ctx = kubectl_context()
    name = _kind_cluster_name(ctx)
    if not name:
        return
    info(f"=== loading bootstrap images into kind cluster {name} ===")
    for image in images:
        run(["kind", "load", "docker-image", image, "--name", name], dry_run=dry_run)

    if dry_run:
        return
    # Force-delete pods whose image matches any of the loaded images so the
    # kubelet resolves the tag freshly rather than using the cached old digest.
    for image in images:
        repo = image.split(":")[0].split("/")[-1]
        result = run(
            [
                "kubectl", "get", "pods", "--all-namespaces",
                "-o", f"jsonpath={{.items[?(@.spec.containers[0].image==\"{image}\")].metadata.name}}",
            ],
            capture=True,
            check=False,
        )
        # Broader approach: label selector if image query returns nothing
        label_result = run(
            [
                "kubectl", "get", "pods", "--all-namespaces",
                "-l", f"app={repo}",
                "-o", "name",
            ],
            capture=True,
            check=False,
        )
        if label_result and label_result.stdout.strip():
            info(f"  force-deleting stale {repo} pods to pick up new image")
            run(
                [
                    "kubectl", "delete", "pods", "--all-namespaces",
                    "-l", f"app={repo}",
                    "--force", "--grace-period=0",
                ],
                check=False,
            )


def _is_k8s_target_cluster(name: str) -> bool:
    return name.startswith("k8s-")


def _is_docker_target_cluster(name: str) -> bool:
    return name.startswith("docker-")


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


def _list_context_files(repo_root: Path, context_dir: Path) -> list[Path]:
    try:
        rel = context_dir.relative_to(repo_root)
    except ValueError as exc:
        die(f"build context {context_dir} is not under repo root {repo_root}: {exc}")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            str(rel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        files = [
            repo_root / line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        return [path for path in files if path.is_file()]
    warn(
        f"git ls-files failed for {context_dir} ({result.stderr.strip() or result.stdout.strip()}); "
        "falling back to walking the directory"
    )
    return [path for path in context_dir.rglob("*") if path.is_file()]


def _stage_imagebuild_context(repo_root: Path, context_dir: Path, dest_dir: Path, dry_run: bool) -> None:
    if not context_dir.is_dir():
        die(f"image build context not found: {context_dir}")
    files = _list_context_files(repo_root, context_dir)
    if not files:
        die(f"image build context has no tracked files: {context_dir}")
    info(f"  stage {context_dir} -> {dest_dir}")
    if dry_run:
        return
    shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        rel = src.relative_to(context_dir)
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _prepare_partition(part: str, dry_run: bool) -> None:
    recipes = IMAGEBUILD_PREPARE_RECIPES.get(part, [])
    if not recipes:
        return
    info(f"[{part}] stage build sources")
    for repo_root, context_dir, dest_dir in recipes:
        _stage_imagebuild_context(repo_root, context_dir, dest_dir, dry_run)


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


def _partition_intents(part: str) -> list[dict]:
    intents_dir = PARTITIONS / part / "intents"
    if not intents_dir.is_dir():
        return []
    docs: list[dict] = []
    for path in sorted(intents_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as e:
            warn(f"skip {path}: {e}")
            continue
        if isinstance(data, dict):
            docs.append(data)
    return docs


def _image_target_clusters(part: str, image_ref: str) -> set[str]:
    repo = _image_repo_name(image_ref)
    clusters: set[str] = set()
    for intent in _partition_intents(part):
        cluster = intent.get("spec", {}).get("target", {}).get("cluster", "")
        if not isinstance(cluster, str) or not cluster.strip():
            continue
        for asset in intent.get("spec", {}).get("assets", []):
            if not isinstance(asset, dict):
                continue
            image = asset.get("properties", {}).get("image")
            if isinstance(image, str) and _image_repo_name(image) == repo:
                clusters.add(cluster.strip())
    return clusters


def _partition_target_clusters(part: str) -> set[str]:
    clusters: set[str] = set()
    for intent in _partition_intents(part):
        cluster = intent.get("spec", {}).get("target", {}).get("cluster", "")
        if isinstance(cluster, str) and cluster.strip():
            clusters.add(cluster.strip())
    return clusters


def _image_distribution_mode(part: str, image_ref: str, ctx: str) -> str:
    clusters = _image_target_clusters(part, image_ref)
    if not clusters:
        clusters = _partition_target_clusters(part)
    if clusters and all(_is_docker_target_cluster(cluster) for cluster in clusters):
        return "local-docker"
    if clusters and any(_is_k8s_target_cluster(cluster) for cluster in clusters):
        return "cluster-load" if _cluster_load_mode(ctx) else "registry-push"
    return "cluster-load" if _cluster_load_mode(ctx) else "registry-push"


def _payload_k8s_path(asset: dict) -> Path | None:
    rel = asset.get("payload", {}).get("k8s")
    if not isinstance(rel, str) or not rel.strip():
        return None
    return ROOT / rel.lstrip("/")


@functools.lru_cache(maxsize=2)
def _kind_nodes_with_labels(dry_run: bool) -> list[tuple[str, dict[str, str]]]:
    result = run(["kubectl", "get", "nodes", "-o", "json"], capture=True, check=False, dry_run=False)
    if not result or result.returncode != 0:
        die("failed to list cluster nodes via kubectl")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        die(f"failed to decode kubectl nodes JSON: {e}")
    nodes: list[tuple[str, dict[str, str]]] = []
    for item in payload.get("items", []):
        name = item.get("metadata", {}).get("name")
        labels = item.get("metadata", {}).get("labels", {})
        if isinstance(name, str) and name.strip():
            nodes.append((name, labels if isinstance(labels, dict) else {}))
    if not nodes:
        die("no cluster nodes found")
    return nodes


def _asset_target_nodes(asset: dict, nodes_with_labels: list[tuple[str, dict[str, str]]]) -> list[str] | None:
    payload_path = _payload_k8s_path(asset)
    if not payload_path or not payload_path.exists():
        return None
    try:
        payload = yaml.safe_load(payload_path.read_text()) or {}
    except Exception as e:
        warn(f"skip {payload_path}: {e}")
        return None
    selector = payload.get("nodeSelector")
    if not isinstance(selector, dict) or not selector:
        return None
    matched = [
        name
        for name, labels in nodes_with_labels
        if all(labels.get(str(key)) == str(value) for key, value in selector.items())
    ]
    if not matched:
        warn(f"no kind nodes matched nodeSelector in {payload_path}")
    return matched


def _partition_image_target_nodes(part: str, image_ref: str, dry_run: bool) -> list[str] | None:
    nodes_with_labels = _kind_nodes_with_labels(dry_run)
    if not nodes_with_labels:
        return None
    matched_assets: list[dict] = []
    for intent in _partition_intents(part):
        cluster = intent.get("spec", {}).get("target", {}).get("cluster", "")
        if not isinstance(cluster, str) or not _is_k8s_target_cluster(cluster):
            continue
        for asset in intent.get("spec", {}).get("assets", []):
            if not isinstance(asset, dict):
                continue
            image = asset.get("properties", {}).get("image")
            if isinstance(image, str) and _image_repo_name(image) == _image_repo_name(image_ref):
                matched_assets.append(asset)
    if not matched_assets:
        return None
    selected: set[str] = set()
    for asset in matched_assets:
        nodes = _asset_target_nodes(asset, nodes_with_labels)
        if nodes is None:
            return [name for name, _labels in nodes_with_labels]
        selected.update(nodes)
    return sorted(selected) if selected else [name for name, _labels in nodes_with_labels]


def _partition_mapping(part: str, registry: str, ctx: str, dry_run: bool) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for tag, _e, _c in BUILD_RECIPES.get(part, []):
        repo = tag.split(":", 1)[0]
        mode = _image_distribution_mode(part, tag, ctx)
        mapping[tag] = _immutable_image_ref(tag, repo, registry, mode in {"cluster-load", "local-docker"}, dry_run)
    for upstream, local in MIRROR_RECIPES.get(part, []):
        repo = local.split(":", 1)[0]
        mode = _image_distribution_mode(part, local, ctx)
        mapping[upstream] = _immutable_image_ref(upstream, repo, registry, mode in {"cluster-load", "local-docker"}, dry_run)
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
    ctx = kubectl_context()
    out: dict[str, list[tuple[Path, list[tuple[str, str]]]]] = {}
    for part in partitions:
        mapping = _partition_mapping(part, registry, ctx, dry_run)
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


def _cluster_load(image: str, dry_run: bool, nodes: list[str] | None = None, *, force: bool = False) -> None:
    if nodes is None:
        nodes = [name for name, _labels in _kind_nodes_with_labels(dry_run)]
    if dry_run:
        info(f"  (dry-run) would cluster-load {image} into: {', '.join(nodes) if nodes else 'no nodes'}")
        return
    if not nodes:
        die("no cluster nodes found")

    nodes_to_load = list(nodes)
    if not force:
        # Check which nodes already have the image (in parallel — avoids N serial kubectl-execs).
        def _needs_load(node: str) -> bool:
            present = subprocess.run(
                ["docker", "exec", node, "ctr", "-n=k8s.io", "images", "inspect", image],
                capture_output=True,
            )
            return present.returncode != 0

        with concurrent.futures.ThreadPoolExecutor() as ex:
            needs_flags = list(ex.map(_needs_load, nodes))

        nodes_to_load = []
        for node, need in zip(nodes, needs_flags):
            if need:
                nodes_to_load.append(node)
            else:
                info(f"  skip {image} (already present) → {node}")

    if not nodes_to_load:
        return

    # Save the image to a temp file once, then stream it into all nodes in parallel.
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        tmp_path = Path(tf.name)

    try:
        save_result = subprocess.run(["docker", "save", "-o", str(tmp_path), image])
        if save_result.returncode != 0:
            die(f"docker save failed for {image}")

        def _load_node(node: str) -> tuple[str, int]:
            info(f"  load {image} → {node}")
            with tmp_path.open("rb") as f:
                proc = subprocess.Popen(
                    ["docker", "exec", "-i", node, "ctr", "-n=k8s.io", "images", "import", "-"],
                    stdin=f,
                )
            proc.wait()
            return node, proc.returncode

        with concurrent.futures.ThreadPoolExecutor() as ex:
            results = list(ex.map(_load_node, nodes_to_load))

        for node, rc in results:
            if rc != 0:
                die(f"cluster-load failed for {image} on {node}")
    finally:
        tmp_path.unlink(missing_ok=True)


# Commands --------------------------------------------------------------------
app = typer.Typer(no_args_is_help=False, help="build/push/stamp partition images")


@app.command("list")
def cmd_list() -> None:
    """List known partitions."""
    for p in PARTITIONS_LIST:
        info(p)


def cmd_build(partitions: list[str], dry_run: bool) -> None:
    for part in partitions:
        _prepare_partition(part, dry_run)
        recipes = BUILD_RECIPES.get(part, [])
        if not recipes and not IMAGEBUILD_PREPARE_RECIPES.get(part):
            info(f"[{part}] no local build step")
            continue
        if not recipes:
            continue
        info(f"[{part}] build")
        for tag, extra, ctx in recipes:
            build_args = _monofs_build_args() if ctx == MONOFS_REPO_DIR else []
            run(["docker", "build", "-t", tag, *extra, *build_args, str(ctx)], dry_run=dry_run)


def cmd_push(partitions: list[str], registry: str, dry_run: bool) -> None:
    ctx = kubectl_context()
    info(f"kubectl context: {ctx or '<none>'}")
    for part in partitions:
        info(f"[{part}] distribute")
        # Pull mirror images before _partition_mapping so the inspect can succeed.
        for upstream, _local in MIRROR_RECIPES.get(part, []):
            run(["docker", "pull", upstream], dry_run=dry_run)
        mapping = _partition_mapping(part, registry, ctx, dry_run)
        for tag, _e, _c in BUILD_RECIPES.get(part, []):
            target = mapping[tag]
            mode = _image_distribution_mode(part, tag, ctx)
            run(["docker", "tag", tag, target], dry_run=dry_run)
            if mode == "cluster-load":
                _cluster_load(target, dry_run, nodes=_partition_image_target_nodes(part, tag, dry_run))
            elif mode == "registry-push":
                run(["docker", "push", target], dry_run=dry_run)
        for upstream, local in MIRROR_RECIPES.get(part, []):
            target = mapping[upstream]
            mode = _image_distribution_mode(part, local, ctx)
            run(["docker", "tag", upstream, target], dry_run=dry_run)
            if mode == "cluster-load":
                _cluster_load(target, dry_run, nodes=_partition_image_target_nodes(part, local, dry_run))
            elif mode == "registry-push":
                run(["docker", "push", target], dry_run=dry_run)


def cmd_stamp(partitions: list[str], registry: str, dry_run: bool) -> None:
    ctx = kubectl_context()
    for part in partitions:
        info(f"[{part}] stamp")
        mapping = _partition_mapping(part, registry, ctx, dry_run)
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
