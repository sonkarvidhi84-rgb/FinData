"""Headless run of the Streamlit app against a migrated fixture warehouse."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from findata.model.db import get_engine, upgrade
from findata.quality import views
from findata.quality.runner import run_quality
from findata.settings import PROJECT_ROOT, get_settings

from .conftest import populate

APP = PROJECT_ROOT / "dashboard" / "app.py"


@pytest.fixture
def app_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    upgrade(url)
    eng = get_engine(url)
    populate(eng)
    # run quality up front with tmp output dirs so the app does not write into docs/
    run_quality(eng, report_dir=tmp_path, model_dir=None)
    eng.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


def test_dashboard_renders_all_tabs_and_reviews(app_db: str) -> None:
    at = AppTest.from_file(str(APP), default_timeout=180).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["Quality Overview", "Anomalies", "Analytics"]
    assert len(at.dataframe) >= 2  # metrics table + anomalies table
    eng = get_engine(app_db)
    reviewed = next(b for b in at.button if b.label == "Mark as reviewed")
    reviewed.click().run()
    assert not at.exception, at.exception
    assert "reviewed" in set(views.anomalies(eng)["status"])
    eng.dispose()
