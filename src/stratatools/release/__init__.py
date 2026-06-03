"""st-release — one-shot per-partition release pipeline.

Thin orchestrator on top of `st-image` (build/push/stamp) plus `guardianctl`
partition tag/push/reconcile/wait. Port of ainfra/scripts/release.
"""
from __future__ import annotations

import os
from typing import Optional

import typer

from stratatools.bootstrap import guardian as bootstrap_guardian
from stratatools.util import PARTITIONS, die, info, run, warn
from stratatools.image import cmd_build, cmd_push, cmd_stamp, is_immutable_image_ref, planned_stamp_changes

app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="One-shot release pipeline for partitions.",
)

GUARDIANCTL = os.environ.get("GUARDIANCTL_BIN", "guardianctl")
DEFAULT_REGISTRY = "localhost:5000"


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _known_release_partitions() -> list[str]:
    parts: list[str] = []
    for entry in sorted(PARTITIONS.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if (entry / "config.yaml").is_file():
            parts.append(entry.name)
    return parts


def _resolve_partitions(partition, all_flag):
    known = _known_release_partitions()
    if all_flag:
        return known
    if not partition:
        die("must pass --partition or --all")
    parts = _dedupe(partition)
    unknown = [p for p in parts if p not in known]
    if unknown:
        die(f"unknown partitions: {unknown}")
    return parts


def _apply_k8s_top_prereq(dry_run: bool) -> None:
    f = PARTITIONS / "k8s-top" / "metrics-reader-rbac-default-sa.yaml"
    if not f.exists():
        warn(f"k8s-top prereq RBAC missing: {f}")
        return
    run(["kubectl", "apply", "-f", str(f)], check=False, dry_run=dry_run)


def _gctl_root_flags() -> list[str]:
    """Return guardian-url + discovery-token flags for guardianctl."""
    url = bootstrap_guardian.guardian_ui_url()
    token = bootstrap_guardian.client_discovery_token()
    flags = []
    if url:
        flags += ["--guardian-url", url]
    if token:
        flags += ["--guardian-discovery-token", token]
    return flags


def _gctl(args: list[str], dry_run: bool):
    """Run guardianctl with args."""
    return run([GUARDIANCTL, *_gctl_root_flags(), *args], check=True, dry_run=dry_run)


def _release_one(
    p: str,
    *,
    bump: bool,
    registry: str,
    skip_build: bool,
    skip_push: bool,
    skip_stamp: bool,
    skip_guardian: bool,
    reconcile: bool,
    wait: bool,
    dry_run: bool,
) -> None:
    info(f"=== release partition: {p} ===")
    pdir = PARTITIONS / p
    if not pdir.is_dir():
        die(f"partition dir not found: {pdir}")

    if p == "k8s-top" and not skip_guardian:
        _apply_k8s_top_prereq(dry_run)

    if bump:
        _gctl(["partition", "tag", "--dir", str(pdir)], dry_run)

    if not skip_build:
        cmd_build([p], dry_run=dry_run)
    if not bump and not dry_run and not skip_stamp:
        changes = planned_stamp_changes([p], registry=registry, dry_run=False).get(p, [])
        immutable_changes: list[tuple[str, str, str]] = []
        for path, file_changes in changes:
            for before, after in file_changes:
                if is_immutable_image_ref(before):
                    immutable_changes.append((str(path.relative_to(PARTITIONS.parent)), before, after))
        if immutable_changes:
            preview = "; ".join(
                f"{path}: {before} -> {after}" for path, before, after in immutable_changes[:3]
            )
            die(
                f"partition {p} would change immutable stamped image refs without --bump. "
                f"Rerun with --bump. Changes: {preview}"
            )
    if not skip_push:
        cmd_push([p], registry=registry, dry_run=dry_run)
    if not skip_stamp:
        cmd_stamp([p], registry=registry, dry_run=dry_run)

    if not skip_guardian:
        _gctl(["partition", "push", "--dir", str(pdir)], dry_run)
        if reconcile:
            _gctl(["partition", "reconcile", "--partition", p], dry_run)
        if wait:
            _gctl(["partition", "wait", "--partition", p], dry_run)
    # TODO: annotate-release OTLP event (skipped in port)


@app.callback(invoke_without_command=True)
def main(
    partition: list[str] = typer.Option(
        None, "--partition", "-p", help="Partition (repeatable)."
    ),
    all_: bool = typer.Option(False, "--all", help="Release every known partition."),
    bump: bool = typer.Option(
        False, "--bump", help="Bump asset version via `guardianctl partition tag`."
    ),
    registry: Optional[str] = typer.Option(
        None, "--registry", help=f"Registry host:port (default: {DEFAULT_REGISTRY})."
    ),
    skip_build: bool = typer.Option(False, "--skip-build"),
    skip_push: bool = typer.Option(False, "--skip-push"),
    skip_stamp: bool = typer.Option(False, "--skip-stamp"),
    skip_guardian: bool = typer.Option(
        False, "--skip-guardian", help="Skip guardianctl push/reconcile/wait."
    ),
    reconcile: bool = typer.Option(
        False, "--reconcile", help="Run `guardianctl partition reconcile` after push (guardian auto-reconciles without this)."
    ),
    wait: bool = typer.Option(
        False, "--wait", help="After reconcile, wait for partition convergence."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands only."),
) -> None:
    parts = _resolve_partitions(partition, all_)
    reg = registry or DEFAULT_REGISTRY
    info(f"releasing partitions: {parts}")
    if any(part in {"guardian-configs", "doctor"} for part in parts):
        bootstrap_guardian.stamp_urls(dry_run)
    for p in parts:
        _release_one(
            p,
            bump=bump,
            registry=reg,
            skip_build=skip_build,
            skip_push=skip_push,
            skip_stamp=skip_stamp,
            skip_guardian=skip_guardian,
            reconcile=reconcile,
            wait=wait,
            dry_run=dry_run,
        )
    info(f"release complete: {parts}")
