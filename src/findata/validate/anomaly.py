"""Shared types for validation rules: the `Anomaly` record and the `RuleContext` passed to rules.

Rules never raise on bad data. They return `Anomaly` records, so one bad row cannot stop a
pipeline run, and every problem ends up queryable in `dq_anomaly`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

Severity = Literal["low", "medium", "high"]
DetectedBy = Literal["rule", "statistical", "ml"]

ANOMALY_COLUMNS = [
    "dataset",
    "rule",
    "severity",
    "record_key",
    "detail",
    "symbol",
    "obs_date",
    "value",
    "detected_by",
]


@dataclass(frozen=True)
class Anomaly:
    """One data-quality finding.

    Attributes:
        dataset: Dataset name the record belongs to.
        rule: Rule or detector name, e.g. `ohlc_sanity`.
        severity: `low`, `medium` or `high`.
        record_key: Stable natural key of the offending record, e.g. `RELIANCE|2024-01-05`.
            (dataset, record_key, rule) is unique in `dq_anomaly`.
        detail: Human-readable explanation, including the numbers involved.
        symbol: Ticker or series id, if any (used for dashboard drill-down).
        obs_date: ISO date of the observation, if any.
        value: The key numeric value that triggered the flag (a return or a z-score, say).
        detected_by: `rule`, `statistical` or `ml`.
    """

    dataset: str
    rule: str
    severity: Severity
    record_key: str
    detail: str
    symbol: str | None = None
    obs_date: str | None = None
    value: float | None = None
    detected_by: DetectedBy = "rule"


def anomalies_to_frame(anomalies: list[Anomaly]) -> pd.DataFrame:
    """Convert anomalies to a DataFrame with the fixed ANOMALY_COLUMNS layout."""
    if not anomalies:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in ANOMALY_COLUMNS})
    return pd.DataFrame([asdict(a) for a in anomalies], columns=ANOMALY_COLUMNS)


@dataclass
class RuleContext:
    """Everything a rule may need beyond the frame under test.

    Attributes:
        dataset: Name of the dataset being validated.
        params: Rule parameters from the dataset YAML (`rules.<rule_name>`).
        trading_days: Expected trading sessions (from `config/calendars/nse.yaml`).
        corporate_actions: Canonical `nse_corporate_actions` frame.
        secondary: Second-source prices (canonical `yahoo_prices`) for cross-checks.
        previous: Previously observed vintage of a versioned (macro) dataset.
    """

    dataset: str
    params: dict[str, Any] = field(default_factory=dict)
    trading_days: list[pd.Timestamp] = field(default_factory=list)
    corporate_actions: pd.DataFrame | None = None
    secondary: pd.DataFrame | None = None
    previous: pd.DataFrame | None = None

    def severity(self, default: Severity) -> Severity:
        sev = self.params.get("severity", default)
        if sev not in ("low", "medium", "high"):
            return default
        return sev  # type: ignore[no-any-return]
