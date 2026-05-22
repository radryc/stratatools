"""st-commit — git commit with optional LLM-generated message.

Thin Python wrapper around the `ai-commit` Go binary built from
ai-commit/ in this workspace.  When --auto is not given the command
is a transparent pass-through to `git commit`.

Build the backend first:
    cd <workspace>/ai-commit && make install

Model (default): google/gemma-4-E2B-it
    Download the pre-converted .litertlm file from the Files tab at
    https://huggingface.co/google/gemma-4-E2B-it
    and point AI_COMMIT_MODEL (or --model) at it.

C library:
    Build liblitertlm_c_cpu.so following
    https://github.com/vladimirvivien/litertlm-go/blob/main/LITERTLM-BUILD.md
    and point AI_COMMIT_LIB (or --lib) at the directory that contains it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Annotated, Optional

import typer

app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    help="git commit with optional LLM-generated message (--auto).",
)

_AI_COMMIT_BIN = os.environ.get("AI_COMMIT_BIN", "ai-commit")

DEFAULT_MODEL_NOTE = (
    "google/gemma-4-E2B-it .litertlm "
    "(https://huggingface.co/google/gemma-4-E2B-it)"
)


def _resolve_binary() -> str:
    """Return the ai-commit binary path, or die with a helpful message."""
    resolved = shutil.which(_AI_COMMIT_BIN)
    if resolved:
        return resolved
    typer.echo(
        f"error: '{_AI_COMMIT_BIN}' not found on PATH.\n"
        "Build and install it first:\n"
        "    cd <workspace>/ai-commit && make install",
        err=True,
    )
    raise typer.Exit(1)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def commit(
    ctx: typer.Context,
    auto: Annotated[bool, typer.Option("--auto", help="Generate message with LLM.")] = False,
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            help=f"Path to .litertlm model file. Default: {DEFAULT_MODEL_NOTE}",
            envvar="AI_COMMIT_MODEL",
        ),
    ] = None,
    lib: Annotated[
        Optional[str],
        typer.Option(
            "--lib",
            help="Directory containing the LiteRT-LM C library.",
            envvar="AI_COMMIT_LIB",
        ),
    ] = None,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", help="Max output tokens for the generated message."),
    ] = 128,
    no_commit: Annotated[
        bool,
        typer.Option("--no-commit", help="Print generated message but do not commit."),
    ] = False,
    edit: Annotated[
        bool,
        typer.Option("--edit", help="Open $EDITOR to review the generated message."),
    ] = False,
) -> None:
    """Commit staged changes, optionally with an LLM-generated message."""
    binary = _resolve_binary()

    cmd: list[str] = [binary]

    if auto:
        cmd.append("--auto")
    if model:
        cmd += ["--model", model]
    if lib:
        cmd += ["--lib", lib]
    if max_tokens != 128:
        cmd += ["--max-tokens", str(max_tokens)]
    if no_commit:
        cmd.append("--no-commit")
    if edit:
        cmd.append("--edit")

    # Pass any extra arguments through to git commit.
    cmd += ctx.args

    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)
