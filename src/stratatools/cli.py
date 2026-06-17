"""Top-level stratatools CLI."""
from __future__ import annotations

import typer

from stratatools.image import app as image_app
from stratatools.aws_setup import app as aws_setup_app
from stratatools.setup import app as setup_app

app = typer.Typer(no_args_is_help=True, help="stratatools — port of ainfra scripts/")
app.add_typer(image_app, name="image")
app.add_typer(aws_setup_app, name="aws-setup")
app.add_typer(setup_app, name="setup")


if __name__ == "__main__":
    app()
