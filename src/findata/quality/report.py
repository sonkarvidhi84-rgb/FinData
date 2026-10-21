"""Markdown summary of a quality run, written to `docs/quality-runs/<run_id>.md`."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from findata.settings import PROJECT_ROOT

DEFAULT_REPORT_DIR = PROJECT_ROOT / "docs" / "quality-runs"
STATUS_ICON = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "n/a": "n/a"}


def _fmt(value: Any, metric: str) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    v = float(value)
    if "hours" in metric:
        return f"{v:.2f} h"
    if metric in ("vintages_stored", "symbols_with_price_basis_mismatch"):
        return f"{v:.0f}"
    return f"{v:.2%}"


def markdown_table(df: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavoured markdown table (no `tabulate` needed)."""
    if df.empty:
        return "_none_\n"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in row) + " |")
    return "\n".join(lines) + "\n"


def write_report(
    run_id: str,
    run_ts: datetime,
    metrics: pd.DataFrame,
    anomalies: pd.DataFrame,
    calibration: pd.DataFrame | None = None,
    contamination: float | None = None,
    out_dir: Path = DEFAULT_REPORT_DIR,
) -> Path:
    """Write the run report and return its path.

    Args:
        run_id: Quality run id (also the file name).
        run_ts: Run timestamp (UTC).
        metrics: `dq_metric`-shaped rows of this run.
        anomalies: Statistical + ML anomalies of this run (ANOMALY_COLUMNS layout).
        calibration: IsolationForest contamination grid results.
        contamination: Chosen contamination.
        out_dir: Output directory.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.md"
    counts = metrics["status"].value_counts().to_dict() if not metrics.empty else {}
    parts = [
        f"# Quality run {run_id}\n",
        f"Run at {run_ts:%Y-%m-%d %H:%M:%S} UTC. "
        + ", ".join(f"{STATUS_ICON[k]}: {counts.get(k, 0)}" for k in STATUS_ICON)
        + ".\n",
        "## Metrics\n",
    ]
    if not metrics.empty:
        m = metrics.copy()
        m["value"] = [_fmt(v, k) for v, k in zip(m["value"], m["metric"], strict=True)]
        m["threshold"] = [_fmt(v, k) for v, k in zip(m["threshold"], m["metric"], strict=True)]
        m["status"] = m["status"].map(STATUS_ICON)
        parts.append(
            markdown_table(m[["dataset", "dimension", "metric", "value", "threshold", "status"]])
        )
    else:
        parts.append("_no metrics_\n")

    parts.append("\n## Statistical and ML anomalies\n")
    if anomalies.empty:
        parts.append("_none_\n")
    else:
        summary = (
            anomalies.groupby(["detected_by", "rule", "severity"]).size().reset_index(name="flags")
        )
        parts.append(markdown_table(summary))
        top = anomalies.sort_values("severity").head(10)[["rule", "severity", "detail"]]
        parts.append("\nFirst flags:\n\n" + markdown_table(top))

    parts.append("\n## IsolationForest calibration\n")
    if calibration is None or calibration.empty:
        parts.append("_not run (too few rows)_\n")
    else:
        cal = calibration.copy()
        for c in ("precision", "recall", "f1"):
            cal[c] = cal[c].map(lambda x: f"{x:.3f}")
        parts.append(
            "Contamination chosen by F1 against hard-rule flags (weak labels; ties go to the "
            f"smaller rate). Chosen: **{contamination}**.\n\n" + markdown_table(cal)
        )
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
