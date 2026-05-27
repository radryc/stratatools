"""Top-level stratatools CLI."""
from __future__ import annotations

import typer

from stratatools.dogfood import app as dogfood_app
from stratatools.image import app as image_app
from stratatools.release import app as release_app
from stratatools.bootstrap import app as bootstrap_app
from stratatools.aws_setup import app as aws_setup_app
from stratatools.setup import app as setup_app

app = typer.Typer(no_args_is_help=True, help="stratatools — port of ainfra scripts/")
app.add_typer(dogfood_app, name="dogfood")
app.add_typer(image_app, name="image")
app.add_typer(release_app, name="release")
app.add_typer(bootstrap_app, name="bootstrap")
app.add_typer(aws_setup_app, name="aws-setup")
app.add_typer(setup_app, name="setup")


if __name__ == "__main__":
    app()
