"""Price adjustment factors from corporate actions (backward, CRSP-style).

Raw exchange prices drop mechanically on an ex-date: a 1:10 split cuts the price by 90%, and a
Rs 20 dividend lowers the open by about Rs 20. Neither is a loss for the holder. To compute
returns that span such events, we multiply every price *before* the ex-date by a factor, so that
the historical series is expressed in today's terms. Raw prices are stored untouched; the factors
live in their own table and the adjusted series is computed in a view (ADR 0003).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, cast

import pandas as pd

OPEN_END = date(9999, 12, 31)
OPEN_START = date(1900, 1, 1)


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows of `df` as plain dicts (typed loosely; values are validated where used)."""
    return cast(list[dict[str, Any]], df.to_dict("records"))


def _num(v: Any) -> float | None:
    """Float value of `v`, or None for missing / non-numeric."""
    if v is None or v is pd.NA:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def event_factors(corporate_actions: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Compute one price-adjustment factor per (symbol, ex_date, action_type).

    * Split / bonus: factor = `ratio` (post price / pre price). A 1:10 split has ratio 0.1, and a
      1:1 bonus has ratio 0.5, because the holder ends with twice the shares at half the price.
    * Cash dividend: factor = 1 - amount / close_before_ex. This removes the cash leaving the
      company from the earlier prices, so the total return is preserved. A dividend whose prior
      close is unknown, or that is at least the price, is skipped (factor would be <= 0).

    Args:
        corporate_actions: Canonical `nse_corporate_actions` frame (symbol, ex_date,
            action_type, ratio, amount).
        prices: Canonical `nse_prices` frame (symbol, trade_date, close).

    Returns:
        Frame with columns symbol, ex_date, action_type, factor. Factors of the same type on the
        same day (e.g. interim + special dividend rows) are multiplied together.
    """
    cols = ["symbol", "ex_date", "action_type", "factor"]
    if corporate_actions.empty:
        return pd.DataFrame(columns=cols)
    ca = corporate_actions[corporate_actions["action_type"].isin(["split", "bonus", "dividend"])]
    px = prices[["symbol", "trade_date", "close"]].dropna().sort_values("trade_date")
    rows: list[dict[str, Any]] = []
    for rec in records(ca):
        ex = pd.Timestamp(rec["ex_date"])
        if rec["action_type"] in ("split", "bonus"):
            ratio = _num(rec.get("ratio"))
            if ratio is None or ratio <= 0:
                continue
            factor = ratio
        else:
            amount = _num(rec.get("amount"))
            if amount is None or amount <= 0:
                continue
            before = px[(px["symbol"] == rec["symbol"]) & (px["trade_date"] < ex)]
            if before.empty:
                continue
            prev_close = float(before["close"].iloc[-1])
            if amount >= prev_close:
                continue
            factor = 1.0 - amount / prev_close
        rows.append(
            {
                "symbol": rec["symbol"],
                "ex_date": ex,
                "action_type": rec["action_type"],
                "factor": factor,
            }
        )
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(rows)
    grouped = out.groupby(["symbol", "ex_date", "action_type"], as_index=False)["factor"].prod()
    return pd.DataFrame(grouped)[cols]


def build_segments(events: pd.DataFrame) -> pd.DataFrame:
    """Turn per-event factors into non-overlapping cumulative-factor segments per symbol.

    For a date d, the cumulative factor is the product of the factors of every event with
    ex_date > d. That number is piecewise constant between ex-dates, so instead of storing
    an adjusted price per row we store one segment per interval:

    ``[1900-01-01, e1-1] -> f1*...*fn``, ``[e1, e2-1] -> f2*...*fn``, ...,
    ``[en, 9999-12-31] -> 1``

    A view then joins each price to its segment (`adjusted_close = close * cumulative_factor`).

    Args:
        events: Frame with symbol, ex_date, factor (several actions on one day are combined).

    Returns:
        Frame with symbol, valid_from, valid_to (dates, inclusive) and cumulative_factor.
    """
    cols = ["symbol", "valid_from", "valid_to", "cumulative_factor"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    ev = events.assign(ex_date=pd.to_datetime(events["ex_date"]).dt.date)
    per_day = ev.groupby(["symbol", "ex_date"], as_index=False)["factor"].prod()
    rows: list[dict[str, Any]] = []
    for symbol in sorted(per_day["symbol"].unique()):
        grp = records(per_day[per_day["symbol"] == symbol].sort_values("ex_date"))
        ex_dates: list[date] = [r["ex_date"] for r in grp]
        factors: list[float] = [float(r["factor"]) for r in grp]
        starts = [OPEN_START, *ex_dates]
        for i, start in enumerate(starts):
            end = ex_dates[i] - timedelta(days=1) if i < len(ex_dates) else OPEN_END
            cum = 1.0
            for f in factors[i:]:
                cum *= f
            rows.append(
                {"symbol": symbol, "valid_from": start, "valid_to": end, "cumulative_factor": cum}
            )
    return pd.DataFrame(rows, columns=cols)
