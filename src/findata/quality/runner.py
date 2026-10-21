"""`findata quality run`: metrics + statistical/ML anomalies -> dq_metric, dq_anomaly, report."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Connection, Engine, Table, create_engine, delete, select

from findata.model.tables import dq_anomaly, dq_metric, metadata
from findata.quality.anomaly import isolation_forest_flags, price_features, rolling_zscore_flags
from findata.quality.metrics import compute_all, load_warehouse
from findata.quality.report import DEFAULT_REPORT_DIR, write_report
from findata.settings import PROJECT_ROOT, get_settings
from findata.validate.anomaly import anomalies_to_frame

DEFAULT_MODEL_DIR = PROJECT_ROOT / "models"
ANOMALY_UPDATE_COLS = ["severity", "detail", "detected_by", "symbol", "obs_date", "value"]


def get_warehouse_engine() -> Engine:
    """Engine for the configured warehouse, migrated to head.

    It uses `findata.model.db` when available, so Alembic owns the schema. Without it,
    it falls back to `create_all` so the quality layer can run standalone.
    """
    try:
        from findata.model.db import get_engine, upgrade
    except ImportError:  # pragma: no cover - model layer always present in the full project
        eng = create_engine(get_settings().db_url)
        metadata.create_all(eng)
        return eng
    eng = get_engine()
    upgrade(eng)
    return eng


def _clean(v: Any) -> Any:
    if isinstance(v, float) and math.isnan(v):
        return None
    if v is pd.NaT:
        return None
    return v


def upsert_rows(
    conn: Connection,
    table: Table,
    rows: Sequence[dict[str, Any]],
    key_cols: list[str],
    update_cols: list[str],
) -> int:
    """NaN-safe wrapper around `findata.model.db.upsert` (dialect-dispatched ON CONFLICT).

    Columns that are not in `update_cols` keep their stored values on conflict. That is how
    an analyst's review `status` survives every re-run.
    """
    from findata.model.db import upsert

    return upsert(
        conn, table, [{k: _clean(v) for k, v in r.items()} for r in rows], key_cols, update_cols
    )


def retire_stale_flags(conn: Connection, current: set[tuple[str, str, str]]) -> int:
    """Delete open statistical/ML flags that this run no longer raises.

    These detectors are recomputed from scratch every run. A flag that disappears (because
    the data was corrected or a detector was improved) should not linger as "open" work.
    Flags an analyst has already reviewed are kept, because their decision is the record.
    Rule-based flags are owned by the load step and are not touched here.

    Returns:
        Number of flags removed.
    """
    rows = conn.execute(
        select(
            dq_anomaly.c.anomaly_id,
            dq_anomaly.c.dataset,
            dq_anomaly.c.record_key,
            dq_anomaly.c.rule,
        )
        .where(dq_anomaly.c.detected_by.in_(["statistical", "ml"]))
        .where(dq_anomaly.c.status == "open")
    ).all()
    stale = [r.anomaly_id for r in rows if (r.dataset, r.record_key, r.rule) not in current]
    if stale:
        conn.execute(delete(dq_anomaly).where(dq_anomaly.c.anomaly_id.in_(stale)))
    return len(stale)


def run_quality(
    engine: Engine | None = None,
    run_id: str | None = None,
    run_ts: datetime | None = None,
    report_dir: Path = DEFAULT_REPORT_DIR,
    model_dir: Path | None = DEFAULT_MODEL_DIR,
) -> str:
    """Compute metrics and anomalies, persist them and write the run report.

    Re-running with the same `run_id` is idempotent: metrics are upserted on
    (run_id, dataset, metric) and anomalies on (dataset, record_key, rule).

    Returns:
        One-line summary.
    """
    eng = engine if engine is not None else get_warehouse_engine()
    ts = run_ts or datetime.now(UTC)
    rid = run_id or ts.strftime("%Y%m%dT%H%M%SZ")
    wh = load_warehouse(eng)

    metrics = compute_all(eng, rid, ts, wh=wh)

    prices = wh["prices"]
    nse = prices[prices["source"] == "nse"]
    yahoo = prices[prices["source"] == "yahoo"]
    stat_flags = []
    ml_contamination: float | None = None
    calibration = pd.DataFrame()
    ml_flags = []
    if not nse.empty:
        feats = price_features(nse, yahoo, wh["actions"])
        stat_flags = rolling_zscore_flags(feats)
        ml = isolation_forest_flags(feats, wh["anomalies"], model_dir=model_dir)
        ml_flags = ml.anomalies
        ml_contamination = ml.contamination
        calibration = ml.calibration
    found = anomalies_to_frame(stat_flags + ml_flags)

    metric_rows = [{str(k): v for k, v in r.items()} for r in metrics.to_dict(orient="records")]
    anomaly_rows: list[dict[str, Any]] = []
    for rec in found.to_dict(orient="records"):
        r = {str(k): v for k, v in rec.items()}
        r["obs_date"] = pd.Timestamp(r["obs_date"]).date() if r["obs_date"] else None
        r.update(status="open", first_seen_ts=ts, last_seen_ts=ts)
        anomaly_rows.append(r)

    with eng.begin() as conn:
        upsert_rows(
            conn,
            dq_metric,
            metric_rows,
            ["run_id", "dataset", "metric"],
            ["run_ts", "dimension", "value", "threshold", "status"],
        )
        upsert_rows(
            conn,
            dq_anomaly,
            anomaly_rows,
            ["dataset", "record_key", "rule"],
            [*ANOMALY_UPDATE_COLS, "last_seen_ts"],
        )

        retired = retire_stale_flags(
            conn, {(r["dataset"], r["record_key"], r["rule"]) for r in anomaly_rows}
        )

    report = write_report(rid, ts, metrics, found, calibration, ml_contamination, report_dir)
    counts = metrics["status"].value_counts().to_dict()
    return (
        f"run {rid}: {len(metrics)} metrics (pass={counts.get('pass', 0)}, "
        f"warn={counts.get('warn', 0)}, fail={counts.get('fail', 0)}), "
        f"{len(stat_flags)} statistical + {len(ml_flags)} ML flags "
        f"({retired} stale open flags retired); report {report}"
    )
