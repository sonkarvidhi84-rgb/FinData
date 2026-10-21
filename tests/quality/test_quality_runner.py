from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, create_engine, insert, text

from findata.model.tables import dq_anomaly
from findata.quality import report, views
from findata.quality.runner import run_quality

from .conftest import RUN_TS, SPIKE_DAY

LATER = datetime(2024, 7, 2, tzinfo=UTC)


def _count(eng: Engine, sql: str, **params: object) -> int:
    with eng.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def test_run_quality_writes_metrics_anomalies_report(wh_engine: Engine, tmp_path: Path) -> None:
    out = run_quality(
        wh_engine, run_id="r1", run_ts=RUN_TS, report_dir=tmp_path, model_dir=tmp_path
    )
    assert out.startswith("run r1:")
    n_metrics = _count(wh_engine, "SELECT COUNT(*) FROM dq_metric WHERE run_id = 'r1'")
    assert n_metrics > 20
    assert _count(wh_engine, "SELECT COUNT(*) FROM dq_anomaly WHERE detected_by = 'statistical'")
    key = f"BBB|{SPIKE_DAY:%Y-%m-%d}"
    sql = "SELECT COUNT(*) FROM dq_anomaly WHERE record_key = :k AND rule = 'zscore_return'"
    assert _count(wh_engine, sql, k=key) == 1
    rpt = tmp_path / "r1.md"
    assert rpt.exists()
    assert "IsolationForest calibration" in rpt.read_text(encoding="utf-8")
    assert (tmp_path / "isolation_forest.joblib").exists()

    # idempotent: same run id -> same row counts
    total = _count(wh_engine, "SELECT COUNT(*) FROM dq_anomaly")
    run_quality(wh_engine, run_id="r1", run_ts=RUN_TS, report_dir=tmp_path, model_dir=None)
    assert _count(wh_engine, "SELECT COUNT(*) FROM dq_metric WHERE run_id = 'r1'") == n_metrics
    assert _count(wh_engine, "SELECT COUNT(*) FROM dq_anomaly") == total


def test_review_status_survives_rerun(wh_engine: Engine, tmp_path: Path) -> None:
    run_quality(wh_engine, run_id="r1", run_ts=RUN_TS, report_dir=tmp_path, model_dir=None)
    a = views.anomalies(wh_engine)
    aid = int(a[a["rule"] == "zscore_return"].iloc[0]["anomaly_id"])
    views.set_anomaly_status(wh_engine, aid, "false_positive")
    run_quality(wh_engine, run_id="r2", run_ts=LATER, report_dir=tmp_path, model_dir=None)
    b = views.anomalies(wh_engine).set_index("anomaly_id").loc[aid]
    assert b["status"] == "false_positive"
    assert pd.Timestamp(b["first_seen_ts"]).date() == RUN_TS.date()
    assert pd.Timestamp(b["last_seen_ts"]).date() == LATER.date()
    assert b["reviewed_ts"] is not None
    with pytest.raises(ValueError):
        views.set_anomaly_status(wh_engine, aid, "bogus")
    views.set_anomaly_status(wh_engine, aid, "open")
    assert views.anomalies(wh_engine).set_index("anomaly_id").loc[aid, "status"] == "open"


def test_views(wh_engine: Engine, tmp_path: Path) -> None:
    assert not views.warehouse_is_empty(wh_engine)
    run_quality(wh_engine, run_id="r1", run_ts=RUN_TS, report_dir=tmp_path, model_dir=None)
    run_quality(wh_engine, run_id="r2", run_ts=LATER, report_dir=tmp_path, model_dir=None)
    assert views.metric_history(wh_engine)["run_id"].nunique() == 2
    assert set(views.latest_metrics(wh_engine)["run_id"]) == {"r2"}
    assert set(views.price_series(wh_engine, "AAA")["source"]) == {"nse", "yahoo"}
    assert views.corporate_actions(wh_engine, "AAA")["action_type"].tolist() == ["split"]


def test_warehouse_is_empty_without_schema(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert views.warehouse_is_empty(eng)


def test_report_handles_empty_inputs(tmp_path: Path) -> None:
    path = report.write_report("r0", RUN_TS, pd.DataFrame(), pd.DataFrame(), None, None, tmp_path)
    txt = path.read_text(encoding="utf-8")
    assert "_no metrics_" in txt
    assert "not run" in txt
    assert report.markdown_table(pd.DataFrame()) == "_none_\n"
    assert report._fmt(None, "x") == "-"
    assert report._fmt(2.5, "lag_hours") == "2.50 h"


def test_stale_open_flags_are_retired_but_reviewed_kept(wh_engine: Engine, tmp_path: Path) -> None:
    stale = {
        "dataset": "nse_prices",
        "rule": "zscore_volume",
        "severity": "medium",
        "detail": "old",
        "detected_by": "statistical",
        "symbol": "AAA",
        "obs_date": None,
        "value": None,
        "first_seen_ts": RUN_TS,
        "last_seen_ts": RUN_TS,
        "reviewed_ts": None,
    }
    with wh_engine.begin() as conn:
        conn.execute(
            insert(dq_anomaly),
            [
                {**stale, "record_key": "AAA|1999-01-01", "status": "open"},
                {**stale, "record_key": "AAA|1999-01-02", "status": "reviewed"},
            ],
        )
    out = run_quality(wh_engine, run_id="r1", run_ts=LATER, report_dir=tmp_path, model_dir=None)
    assert "1 stale open flags retired" in out
    keys = set(views.anomalies(wh_engine)["record_key"])
    assert "AAA|1999-01-01" not in keys
    assert "AAA|1999-01-02" in keys
    # rule-based flags are never retired by the quality run
    assert _count(wh_engine, "SELECT COUNT(*) FROM dq_anomaly WHERE detected_by = 'rule'") == 2
