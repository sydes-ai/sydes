"""Routes CLI command group for future endpoint route discovery commands."""

import typer

app = typer.Typer(help="Inspect discovered routes.")


@app.callback(invoke_without_command=True)
def routes() -> None:
    """Run placeholder routes command."""
    typer.echo("Not implemented yet")
