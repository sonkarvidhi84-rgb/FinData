"""Command-line entry point: `findata ingest|samples|validate|load|quality|dashboard`.

Heavy modules are imported inside commands so `findata --help` stays fast.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime

import typer

from findata.settings import PROJECT_ROOT

app = typer.Typer(help="FinData Quality Pipeline", no_args_is_help=True)
quality_app = typer.Typer(help="Data-quality metrics and anomaly detection", no_args_is_help=True)
samples_app = typer.Typer(help="Offline sample data", no_args_is_help=True)
app.add_typer(quality_app, name="quality")
app.add_typer(samples_app, name="samples")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.command()
def ingest(
    source: str = typer.Argument("all", help="Source name (nse, yahoo, fbil, ...) or 'all'"),
    from_: str = typer.Option("2024-01-01", "--from", help="Start date YYYY-MM-DD"),
    to: str = typer.Option("2024-06-30", "--to", help="End date YYYY-MM-DD"),
    symbols: str | None = typer.Option(None, help="Comma-separated tickers; default = universe"),
) -> None:
    """Fetch raw data from public sources into data/raw (rate-limited, idempotent)."""
    from findata.ingest.runner import run_ingest

    syms = [s.strip() for s in symbols.split(",")] if symbols else None
    summary = run_ingest(source, _parse_date(from_), _parse_date(to), syms)
    for name, rows in summary.items():
        typer.echo(f"{name:<24} {rows:>8} rows")


@samples_app.command("build")
def samples_build(
    from_: str = typer.Option("2024-01-01", "--from"),
    to: str = typer.Option("2024-06-30", "--to"),
) -> None:
    """Truncate data/raw to the universe x date range and write data/samples/*.csv."""
    from findata.ingest.samples import build_samples

    for name, rows in build_samples(_parse_date(from_), _parse_date(to)).items():
        typer.echo(f"{name:<24} {rows:>8} rows")


@app.command()
def validate(
    dataset: str = typer.Argument("all", help="Dataset name or 'all'"),
    mode: str = typer.Option("auto", help="raw | samples | auto"),
) -> None:
    """Validate datasets, write data/staging/*.parquet and anomalies.parquet."""
    from findata.validate.engine import run_validation

    summary = run_validation(dataset, mode)
    for name, counts in summary.items():
        typer.echo(f"{name:<24} rows={counts['rows']:>7} anomalies={counts['anomalies']:>5}")


@app.command()
def load(mode: str = typer.Option("auto", help="raw | samples | auto")) -> None:
    """Migrate the DB, validate if needed, then load staging into the warehouse (upserts)."""
    from findata.model.loaders import run_load

    for table, rows in run_load(mode).items():
        typer.echo(f"{table:<26} {rows:>8} rows")


@quality_app.command("run")
def quality_run() -> None:
    """Compute quality metrics + statistical/ML anomalies and write dq_* tables and a report."""
    from findata.quality.runner import run_quality

    result = run_quality()
    typer.echo(result)


@app.command()
def dashboard() -> None:
    """Launch the Streamlit dashboard."""
    app_path = PROJECT_ROOT / "dashboard" / "app.py"
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path)]))
