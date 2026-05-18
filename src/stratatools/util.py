"""Tiny shared helpers. Keep this file small."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[2]            # stratatools repo root
PARTITIONS = ROOT / "partitions"
TEMPLATES = Path(__file__).resolve().parent / "templates"


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess | None:
    """Run a command. Prints `+ <cmd>` first. Returns None when dry_run."""
    print("+ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr, flush=True)
    if dry_run:
        return None
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True, cwd=cwd
    )


def kubectl_context() -> str:
    r = run(["kubectl", "config", "current-context"], capture=True, check=False)
    return r.stdout.strip() if r and r.returncode == 0 else ""
