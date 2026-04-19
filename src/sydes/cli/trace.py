"""Trace CLI command group for future API flow tracing commands."""

import typer

app = typer.Typer(help="Trace API flows from code.")


@app.callback(invoke_without_command=True)
def trace() -> None:
    """Run placeholder trace command."""
    typer.echo("Not implemented yet")
