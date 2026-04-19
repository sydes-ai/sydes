"""Root Typer application for the Sydes command-line interface."""

import typer

from sydes.cli.routes import routes_command
from sydes.cli.trace import trace_command

app = typer.Typer(help="Sydes CLI")
app.command(name="trace")(trace_command)
app.command(name="routes")(routes_command)

if __name__ == "__main__":
    app()
