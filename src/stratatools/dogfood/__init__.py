"""st-dogfood — ingest the local Strata repo set into MonoFS."""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess
from pathlib import Path

import typer

from stratatools.setup import REPOS as SIBLING_REPOS
from stratatools.util import ROOT, die, info, run, warn

app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="Ingest the local Strata repository set into MonoFS.",
)

AINFRA = ROOT.parent
MONOFS_REPO_DIR = Path(os.environ.get("MONOFS_REPO_DIR", str(AINFRA / "monofs"))).expanduser()
LOCAL_BIN_DIR = Path(
    os.environ.get("STRATATOOLS_BIN_DIR", str(Path.home() / "bin"))
).expanduser()
DEFAULT_ROUTER = os.environ.get("MONOFS_ROUTER", "localhost:9090")
GITHUB_SSH_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$"),
)


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


def _discover_repos() -> list[RepoIngestSpec]:
    inventory: list[tuple[str, Path]] = [("stratatools", ROOT)]
    inventory.extend((name, AINFRA / name) for name in SIBLING_REPOS)

    repos: list[RepoIngestSpec] = []
    seen: set[str] = set()
    for name, repo_dir in inventory:
        if name in seen:
            continue
        seen.add(name)

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
                f"--source-id={repo.name}",
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