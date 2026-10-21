"""Command-line entry point: `findata ingest|validate|load|quality|dashboard`."""

from __future__ import annotations

import typer

app = typer.Typer(help="FinData Quality Pipeline", no_args_is_help=True)
quality_app = typer.Typer(help="Data-quality metrics and anomaly detection", no_args_is_help=True)
app.add_typer(quality_app, name="quality")


@app.command()
def ingest(source: str = typer.Argument("all", help="Source name or 'all'")) -> None:
    """Fetch raw data from public sources into data/raw."""
    typer.echo(f"ingest {source}: not implemented yet")


@app.command()
def validate(dataset: str = typer.Argument("all")) -> None:
    """Validate raw data and write staging + anomalies."""
    typer.echo(f"validate {dataset}: not implemented yet")


@app.command()
def load() -> None:
    """Load staging into the warehouse."""
    typer.echo("load: not implemented yet")


@quality_app.command("run")
def quality_run() -> None:
    """Compute quality metrics."""
    typer.echo("quality run: not implemented yet")


@app.command()
def dashboard() -> None:
    """Launch the Streamlit dashboard."""
    typer.echo("dashboard: not implemented yet")
