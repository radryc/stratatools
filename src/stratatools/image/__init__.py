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
ST_ROOT = Path(os.environ.get("ST_ROOT", ROOT.parent))


def _repo(name: str) -> Path:
    override = os.environ.get("PARTITION_REPO")
    if override:
        return Path(override)
    return ST_ROOT / name

PARTITIONS_LIST: list[str] = [
    "guardian-configs", "opentelemetry", "k8s-top",
    "doctor", "monitoring", "dev-workspace", "agent", "lb-agent", "lolipop",
]

# Functions needed early for constant definitions --------------------------------
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
        or _git_output(_repo("monofs"), ["rev-parse", "--short=12", "HEAD"], "unknown")
    )
    dirty = _git_output(_repo("monofs"), ["status", "--porcelain"], "")
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


# Each recipe: (image_tag, extra_docker_build_args, build_context_dir)
BUILD_RECIPES: dict[str, list[tuple[str, list[str], Path]]] = {
    "lolipop": [
        ("lolipop-frontend:latest", [], _repo("lolipop") / "frontend"),
        ("lolipop-backend:latest", [], _repo("lolipop") / "backend"),
        ("lolipop-qwen-tts:latest", [], _repo("lolipop") / "qwen-tts-service"),
        ("lolipop-lora-trainer:latest", [], _repo("lolipop") / "lora-trainer"),
        ("lolipop-wangp:latest", [], _repo("lolipop") / "wan2gp-docker"),
    ],
    "dev-workspace": [
        ("monofs-client:dev-base", ["--target", "client"], _repo("monofs")),
        ("dev-workspace-opencode:latest",
         ["-f", str(ROOT / "images" / "dev-workspace-vscode" / "Dockerfile"),
          "--build-arg", "BASE_IMAGE=monofs-client:dev-base"],
         ST_ROOT),
    ],
}

# Each tar recipe: (local_tag, dockerfile_rel, build_args, context_dir, tar_dest_dir, tar_filename, source_image, build_contexts)
# build_contexts: dict of BuildKit named context name -> path (for COPY --from=<name>), or None
IMAGE_TAR_RECIPES: dict[str, list[tuple[str, str, list[str], Path, Path, str, str, dict[str, Path] | None]]] = {
    "guardian-configs": [
        ("guardian:latest", "Dockerfile", _monofs_build_args(),
         _repo("guardian"),
         PARTITIONS / "guardian-configs" / "payloads" / "images",
         "guardian.tar", "guardian:latest",
         {"monofs": _repo("monofs"), "kvs": _repo("kvs")}),
        ("guardian-pusher-k8s:latest", "Dockerfile.pusher-k8s", [],
         _repo("guardian"),
         PARTITIONS / "guardian-configs" / "payloads" / "images",
         "guardian-pusher-k8s.tar", "guardian-pusher-k8s:latest",
         {"monofs": _repo("monofs"), "kvs": _repo("kvs")}),
        ("guardian-pusher-aws:latest", "Dockerfile.pusher-aws", [],
         _repo("guardian"),
         PARTITIONS / "guardian-configs" / "payloads" / "images",
         "guardian-pusher-aws.tar", "guardian-pusher-aws:latest",
         {"monofs": _repo("monofs"), "kvs": _repo("kvs")}),
        ("guardian-pusher-docker:latest", "Dockerfile.pusher-docker", [],
         _repo("guardian"),
         PARTITIONS / "guardian-configs" / "payloads" / "images",
         "guardian-pusher-docker.tar", "guardian-pusher-docker:latest",
         {"monofs": _repo("monofs"), "kvs": _repo("kvs")}),
        ("lb:latest", "Dockerfile", [],
         _repo("lb"),
         PARTITIONS / "guardian-configs" / "payloads" / "images",
         "lb.tar", "lb:latest",
         None),
    ],
    "dev-workspace": [
        ("lb:latest", "Dockerfile", [],
         _repo("lb"),
         PARTITIONS / "dev-workspace" / "payloads" / "images",
         "lb.tar", "lb:latest",
         None),
    ],
    "monitoring": [
        ("lb:latest", "Dockerfile", [],
         _repo("lb"),
         PARTITIONS / "monitoring" / "payloads" / "images",
         "lb.tar", "lb:latest",
         None),
    ],
    "k8s-top": [
        ("k8s-top:latest", "k8s-top/Dockerfile", [],
         _repo("k8s-top").parent,
         PARTITIONS / "k8s-top" / "payloads" / "images",
         "k8s-top.tar", "k8s-top:latest",
         None),
    ],
    "agent": [
        ("lagent-llm:latest", "Dockerfile", [],
         _repo("agent") / "llm",
         PARTITIONS / "agent" / "payloads" / "images",
         "lagent-llm.tar", "lagent-llm:latest",
         None),
        ("lagent-backend:latest", "Dockerfile", [],
         _repo("agent") / "backend",
         PARTITIONS / "agent" / "payloads" / "images",
         "lagent-backend.tar", "lagent-backend:latest",
         None),
        ("lagent-frontend:latest", "Dockerfile", [],
         _repo("agent") / "frontend",
         PARTITIONS / "agent" / "payloads" / "images",
         "lagent-frontend.tar", "lagent-frontend:latest",
         None),
    ],
    "doctor": [
        ("doctor:latest", "Dockerfile", [],
         _repo("doctor"),
         PARTITIONS / "doctor" / "payloads" / "images",
         "doctor.tar", "doctor:latest",
         {"monofs": _repo("monofs")}),
        ("lb:latest", "Dockerfile", [],
         _repo("lb"),
         PARTITIONS / "doctor" / "payloads" / "images",
         "lb.tar", "lb:latest",
         None),
    ],
}

# Each prepare recipe: (git_repo_root, build_context_dir, staged_dest_dir)
IMAGEBUILD_PREPARE_RECIPES: dict[str, list[tuple[Path, Path, Path]]] = {
    "opentelemetry": [
        (_repo("lb"), _repo("lb"), PARTITIONS / "opentelemetry" / "payloads" / "sources" / "lb"),
    ],
    "lb-agent": [
        (_repo("lb"), _repo("lb"), PARTITIONS / "lb-agent" / "payloads" / "sources" / "lb"),
    ],
}

OTEL_UPSTREAM = "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.108.0"
GRAFANA_UPSTREAM = "mirror.gcr.io/grafana/grafana:13.0.0"

# Partitions whose Dockerfiles use BuildKit named build contexts (COPY --from=<name>)
# that need rewriting to local directory paths for kaniko ImageBuild.
_KANIKO_BUILD_CONTEXT_PATCH: dict[str, list[str]] = {
    "guardian-configs": ["monofs", "kvs"],
    "doctor": ["monofs"],
}

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


def _stage_imagebuild_context(repo_root: Path, context_dir: Path, dest_dir: Path, dry_run: bool, *, append: bool = False) -> None:
    if not context_dir.is_dir():
        die(f"image build context not found: {context_dir}")
    files = _list_context_files(repo_root, context_dir)
    if not files:
        die(f"image build context has no tracked files: {context_dir}")
    info(f"  stage {context_dir} -> {dest_dir}")
    if dry_run:
        return
    if not append:
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        rel = src.relative_to(context_dir)
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _patch_build_contexts(part: str, contexts: list[str], dry_run: bool) -> None:
    part_dir = PARTITIONS / part / "payloads" / "sources"
    if not part_dir.is_dir():
        return
    for dockerfile_path in sorted(part_dir.rglob("Dockerfile*")):
        if not dockerfile_path.is_file():
            continue
        content = dockerfile_path.read_text()
        modified = False
        for ctx in contexts:
            old = f"COPY --from={ctx} "
            new = f"COPY {ctx}/"
            if old in content:
                content = content.replace(old, new)
                modified = True
        if modified:
            if dry_run:
                info(f"  (dry-run) would patch build contexts in {dockerfile_path.relative_to(ROOT)}")
            else:
                dockerfile_path.write_text(content)
                info(f"  patched build contexts in {dockerfile_path.relative_to(ROOT)}")


def _prepare_partition(part: str, dry_run: bool) -> None:
    recipes = IMAGEBUILD_PREPARE_RECIPES.get(part, [])
    if not recipes:
        return
    info(f"[{part}] stage build sources")
    for repo_root, context_dir, dest_dir in recipes:
        _stage_imagebuild_context(repo_root, context_dir, dest_dir, dry_run)
    contexts = _KANIKO_BUILD_CONTEXT_PATCH.get(part)
    if contexts:
        _patch_build_contexts(part, contexts, dry_run)


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
        local_recipes = BUILD_RECIPES.get(part, [])
        tar_recipes = IMAGE_TAR_RECIPES.get(part, [])
        if not local_recipes and not tar_recipes and not IMAGEBUILD_PREPARE_RECIPES.get(part):
            info(f"[{part}] no local build step")
            continue
        if local_recipes or tar_recipes:
            info(f"[{part}] build")
        for tag, extra, ctx in local_recipes:
            build_args = _monofs_build_args() if ctx == _repo("monofs") else []
            run(["docker", "build", "-t", tag, *extra, *build_args, str(ctx)], dry_run=dry_run)
        for tag, dockerfile, build_args, ctx, tar_dest_dir, tar_filename, source_image, build_contexts in tar_recipes:
            dockerfile_path = ctx / dockerfile
            full_args = ["docker", "build", "-t", tag, "-f", str(dockerfile_path)]
            if build_contexts:
                for name, path in build_contexts.items():
                    full_args.extend(["--build-context", f"{name}={path}"])
            full_args.extend(build_args)
            full_args.append(str(ctx))
            run(full_args, dry_run=dry_run)
            tar_dest = tar_dest_dir / tar_filename
            info(f"  save {tag} -> {tar_dest}")
            if not dry_run:
                tar_dest.parent.mkdir(parents=True, exist_ok=True)
            run(["docker", "save", "-o", str(tar_dest), tag], dry_run=dry_run)


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
