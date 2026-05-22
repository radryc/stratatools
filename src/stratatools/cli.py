"""Top-level stratatools CLI."""
from __future__ import annotations

import typer

from stratatools.dogfood import app as dogfood_app
from stratatools.image import app as image_app
from stratatools.release import app as release_app
from stratatools.bootstrap import app as bootstrap_app
from stratatools.setup import app as setup_app
from stratatools.commit import app as commit_app

app = typer.Typer(no_args_is_help=True, help="stratatools — port of ainfra scripts/")
app.add_typer(dogfood_app, name="dogfood")
app.add_typer(image_app, name="image")
app.add_typer(release_app, name="release")
app.add_typer(bootstrap_app, name="bootstrap")
app.add_typer(setup_app, name="setup")
app.add_typer(commit_app, name="commit")


if __name__ == "__main__":
    app()
