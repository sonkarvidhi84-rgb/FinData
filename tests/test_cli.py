"""End-to-end CLI test: committed samples -> validate -> load -> quality, fully offline."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from findata import cli
from findata.settings import PROJECT_ROOT, get_settings

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    shutil.copytree(PROJECT_ROOT / "data" / "samples", tmp_path / "samples")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ingest", "validate", "load", "quality", "dashboard", "samples"):
        assert cmd in result.output


def test_offline_pipeline_end_to_end(data_dir: Path) -> None:
    res = runner.invoke(cli.app, ["validate", "all", "--mode", "samples"])
    assert res.exit_code == 0, res.output
    assert "nse_prices" in res.output

    res = runner.invoke(cli.app, ["load", "--mode", "samples"])
    assert res.exit_code == 0, res.output
    assert "fact_price_daily" in res.output

    out = data_dir / "out"
    res = runner.invoke(
        cli.app, ["quality", "run", "--report-dir", str(out), "--model-dir", str(out)]
    )
    assert res.exit_code == 0, res.output
    assert list(out.glob("*.md")), "quality report written"

    engine = sa.create_engine(get_settings().db_url)
    with engine.connect() as conn:
        prices = conn.execute(sa.text("select count(*) from fact_price_daily")).scalar_one()
        metrics = conn.execute(sa.text("select count(*) from dq_metric")).scalar_one()
        nestle = conn.execute(
            sa.text(
                "select count(*) from map_security_identifier m join dim_security s "
                "using (security_id) where s.symbol='NESTLEIND' and m.id_type='ISIN'"
            )
        ).scalar_one()
    engine.dispose()
    assert prices == 4860
    assert metrics > 0
    assert nestle == 2  # ISIN changed at the 2024-01-05 split: two validity windows

    # idempotent: a second load must not add rows
    res = runner.invoke(cli.app, ["load", "--mode", "samples"])
    assert res.exit_code == 0
    engine = sa.create_engine(get_settings().db_url)
    with engine.connect() as conn:
        assert conn.execute(sa.text("select count(*) from fact_price_daily")).scalar_one() == 4860
    engine.dispose()


def test_ingest_and_samples_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_ingest(
        source: str, start: date, end: date, symbols: list[str] | None
    ) -> dict[str, int]:
        calls["ingest"] = (source, start, end, symbols)
        return {"nse_prices": 3}

    def fake_samples(start: date, end: date) -> dict[str, int]:
        calls["samples"] = (start, end)
        return {"nse_prices": 2}

    monkeypatch.setattr("findata.ingest.runner.run_ingest", fake_ingest)
    monkeypatch.setattr("findata.ingest.samples.build_samples", fake_samples)
    res = runner.invoke(
        cli.app,
        ["ingest", "nse", "--from", "2024-01-01", "--to", "2024-01-31", "--symbols", "TCS,INFY"],
    )
    assert res.exit_code == 0, res.output
    assert calls["ingest"] == ("nse", date(2024, 1, 1), date(2024, 1, 31), ["TCS", "INFY"])
    res = runner.invoke(cli.app, ["samples", "build"])
    assert res.exit_code == 0
    assert calls["samples"] == (date(2024, 1, 1), date(2024, 6, 30))


def test_dashboard_launches_streamlit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda args: seen.append(args) or 0)
    res = runner.invoke(cli.app, ["dashboard"])
    assert res.exit_code == 0
    assert seen and seen[0][-1].endswith("app.py")
