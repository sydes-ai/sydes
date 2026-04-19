"""Root Typer application for the Sydes command-line interface."""

import typer

from sydes.cli.routes import app as routes_app
from sydes.cli.trace import app as trace_app

app = typer.Typer(help="Sydes CLI")
app.add_typer(trace_app, name="trace")
app.add_typer(routes_app, name="routes")

if __name__ == "__main__":
    app()
