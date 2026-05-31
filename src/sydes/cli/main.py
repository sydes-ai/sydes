"""Root Typer application for the Sydes command-line interface."""

from importlib import metadata

import typer

from sydes.cli.export import export_command
from sydes.cli.routes import routes_command
from sydes.cli.trace import trace_command

app = typer.Typer(help="Sydes CLI")


def _resolve_cli_version() -> str:
    """Resolve Sydes package version using installed metadata with safe fallback."""
    try:
        return metadata.version("sydes")
    except metadata.PackageNotFoundError:
        try:
            from sydes import __version__

            return __version__
        except Exception:
            return "unknown"
    except Exception:
        return "unknown"


def _version_callback(value: bool) -> None:
    """Print CLI version and exit when --version is provided."""
    if not value:
        return
    typer.echo(f"sydes {_resolve_cli_version()}")
    raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show Sydes version and exit.",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    """Top-level Sydes CLI options."""
    _ = version


app.command(name="trace")(trace_command)
app.command(name="routes")(routes_command)
app.command(name="export")(export_command)

if __name__ == "__main__":
    app()
