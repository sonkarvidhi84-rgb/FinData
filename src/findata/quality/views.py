"""Read/write helpers behind the dashboard, kept out of Streamlit so they are testable."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import Engine, func, select, text, update

from findata.model.tables import dq_anomaly, fact_price_daily

REVIEW_STATUSES = ("open", "reviewed", "false_positive")


def warehouse_is_empty(engine: Engine) -> bool:
    """True if there is no price data (or no schema) yet."""
    try:
        with engine.connect() as conn:
            n = conn.execute(select(func.count()).select_from(fact_price_daily)).scalar_one()
    except Exception:
        return True
    return int(n) == 0


def metric_history(engine: Engine) -> pd.DataFrame:
    """All dq_metric rows, with parsed run_ts."""
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM dq_metric ORDER BY run_ts"), conn)
    df["run_ts"] = pd.to_datetime(df["run_ts"], utc=True, errors="coerce")
    return df


def latest_metrics(engine: Engine) -> pd.DataFrame:
    """dq_metric rows of the most recent run."""
    hist = metric_history(engine)
    if hist.empty:
        return hist
    last = str(hist.sort_values("run_ts")["run_id"].iloc[-1])
    latest: pd.DataFrame = hist[hist["run_id"] == last].reset_index(drop=True)
    return latest


def anomalies(engine: Engine) -> pd.DataFrame:
    """All dq_anomaly rows."""
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM dq_anomaly"), conn)
    df["obs_date"] = pd.to_datetime(df["obs_date"], errors="coerce")
    return df


def price_series(engine: Engine, symbol: str) -> pd.DataFrame:
    """NSE and Yahoo daily prices for one symbol."""
    sql = text(
        """SELECT p.trade_date, p.source, p.open, p.high, p.low, p.close, p.volume
           FROM fact_price_daily p JOIN dim_security s ON s.security_id = p.security_id
           WHERE s.symbol = :symbol ORDER BY p.trade_date"""
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"symbol": symbol})
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def corporate_actions(engine: Engine, symbol: str) -> pd.DataFrame:
    """Corporate actions for one symbol."""
    sql = text(
        """SELECT a.ex_date, a.action_type, a.subject, a.ratio, a.amount
           FROM fact_corporate_action a JOIN dim_security s ON s.security_id = a.security_id
           WHERE s.symbol = :symbol ORDER BY a.ex_date"""
    )
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"symbol": symbol})
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def set_anomaly_status(engine: Engine, anomaly_id: int, status: str) -> None:
    """Record an analyst's review decision on one anomaly.

    Raises:
        ValueError: on an unknown status (this is user input, not data, so raising is right).
    """
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status must be one of {REVIEW_STATUSES}")
    reviewed = None if status == "open" else datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            update(dq_anomaly)
            .where(dq_anomaly.c.anomaly_id == anomaly_id)
            .values(status=status, reviewed_ts=reviewed)
        )
