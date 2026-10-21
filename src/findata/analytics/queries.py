"""Cross-dataset analytical queries used by the dashboard's Analytics tab.

Each query joins at least two datasets in the warehouse. That is the point of modelling them
into one schema with shared dimensions (security, calendar, currency).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from findata.model.adjustments import records

ADJUSTED_PRICES_SQL = """
SELECT v.security_id, v.symbol, s.sector, v.trade_date, v.close, v.adjusted_close
FROM v_price_adjusted v
JOIN dim_security s ON s.security_id = v.security_id
WHERE v.adjusted_close IS NOT NULL
ORDER BY v.symbol, v.trade_date
"""

DIVIDENDS_SQL = """
SELECT ca.security_id, ca.ex_date, ca.record_date, ca.amount, ca.subject
FROM fact_corporate_action ca
WHERE ca.action_type = 'dividend'
"""

USDINR_SQL = """
SELECT fx.rate_date, fx.rate / fx.units AS inr_per_usd
FROM fact_fx_rate fx
JOIN dim_currency b ON b.currency_id = fx.base_currency_id
JOIN dim_currency q ON q.currency_id = fx.quote_currency_id
WHERE b.code = 'USD' AND q.code = 'INR'
ORDER BY fx.rate_date
"""

MACRO_LATEST_SQL = """
SELECT m.obs_date, m.value
FROM fact_macro_series m
JOIN dim_macro_series d ON d.series_id = m.series_id
WHERE d.code = :code
  AND m.vintage_date = (
      SELECT MAX(m2.vintage_date) FROM fact_macro_series m2
      WHERE m2.series_id = m.series_id AND m2.obs_date = m.obs_date
  )
ORDER BY m.obs_date
"""


def _read(engine: Engine, sql: str, **params: Any) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def adjusted_prices(engine: Engine) -> pd.DataFrame:
    """Split/dividend-adjusted NSE closes with sector, one row per (symbol, trade_date)."""
    df = _read(engine, ADJUSTED_PRICES_SQL)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def dividend_event_returns(engine: Engine, window: int = 5) -> pd.DataFrame:
    """Sector-adjusted `window`-session returns around each dividend ex-date.

    The event return runs from the close of the session *before* the ex-date to the close
    `window` sessions later, computed on adjusted prices so the mechanical ex-dividend drop
    is not mistaken for a loss. Subtracting the equal-weight return of the stock's sector
    peers over the same dates strips out market and sector moves. What remains (the
    "excess" return) is what the event itself is associated with.

    Args:
        engine: Warehouse engine.
        window: Sessions after the ex-date included in the event window.

    Returns:
        One row per dividend event with stock, sector and excess returns.
    """
    cols = [
        "symbol",
        "sector",
        "ex_date",
        "record_date",
        "amount",
        "start_date",
        "end_date",
        "stock_return",
        "sector_return",
        "excess_return",
        "n_peers",
    ]
    px = adjusted_prices(engine)
    divs = _read(engine, DIVIDENDS_SQL)
    if px.empty or divs.empty:
        return pd.DataFrame(columns=cols)
    divs["ex_date"] = pd.to_datetime(divs["ex_date"])
    wide = px.pivot_table(index="trade_date", columns="symbol", values="adjusted_close")
    sessions = wide.index
    meta = px.drop_duplicates("security_id")
    symbol_of = {int(k): str(v) for k, v in zip(meta["security_id"], meta["symbol"], strict=True)}
    sector_of = {str(k): v for k, v in zip(meta["symbol"], meta["sector"], strict=True)}
    rows = []
    for ev in records(divs):
        sym = symbol_of.get(int(ev["security_id"]))
        if sym is None:
            continue
        pos = int(sessions.searchsorted(pd.Timestamp(str(ev["ex_date"]))))
        if pos == 0 or pos + window - 1 >= len(sessions):
            continue
        t0, t1 = sessions[pos - 1], sessions[pos + window - 1]
        rets = wide.loc[t1] / wide.loc[t0] - 1.0
        if pd.isna(rets.get(sym)):
            continue
        sector = sector_of.get(sym)
        peers = [s for s in rets.index if s != sym and sector_of.get(s) == sector]
        if not peers:
            peers = [s for s in rets.index if s != sym]
        peer_ret = rets[peers].dropna()
        sector_ret = float(peer_ret.mean()) if len(peer_ret) else float("nan")
        stock_ret = float(rets[sym])
        rows.append(
            {
                "symbol": sym,
                "sector": sector,
                "ex_date": pd.Timestamp(str(ev["ex_date"])).date(),
                "record_date": ev["record_date"],
                "amount": ev["amount"],
                "start_date": t0.date(),
                "end_date": t1.date(),
                "stock_return": stock_ret,
                "sector_return": sector_ret,
                "excess_return": stock_ret - sector_ret,
                "n_peers": len(peer_ret),
            }
        )
    return (
        pd.DataFrame(rows, columns=cols).sort_values(["ex_date", "symbol"]).reset_index(drop=True)
    )


def usd_inr(engine: Engine) -> pd.Series:
    """INR per USD reference rate (FBIL) indexed by date."""
    fx = _read(engine, USDINR_SQL)
    fx["rate_date"] = pd.to_datetime(fx["rate_date"])
    return fx.set_index("rate_date")["inr_per_usd"].astype(float)


def _asof(series: pd.Series, when: pd.Timestamp) -> float | None:
    """Last value on or before `when`, else the first value after it (None if empty)."""
    before = series[series.index <= when]
    if not before.empty:
        return float(before.iloc[-1])
    after = series[series.index > when]
    return float(after.iloc[0]) if not after.empty else None


def inr_hedged_vs_unhedged(engine: Engine) -> pd.DataFrame:
    """Per-security return for a USD-based investor, unhedged vs currency-hedged.

    An unhedged USD investor earns the local INR return *and* the move in the rupee:
    ``(1 + r_INR) * (FX_start / FX_end) - 1`` with FX quoted as INR per USD (a rising rate
    means a weaker rupee and erodes the USD return). A fully hedged investor locks in the
    currency, so we approximate the hedged return by the local INR return. This ignores
    forward points, i.e. the interest-rate differential the hedge costs or earns; see
    known issues.

    Returns:
        One row per symbol with INR, unhedged-USD and hedged-USD returns over the common period.
    """
    cols = [
        "symbol",
        "sector",
        "start_date",
        "end_date",
        "inr_return",
        "fx_start",
        "fx_end",
        "usd_unhedged_return",
        "usd_hedged_return",
        "hedge_benefit",
    ]
    px = adjusted_prices(engine)
    fx = usd_inr(engine)
    if px.empty or fx.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for sym, grp in px.groupby("symbol", sort=True):
        grp = grp.sort_values("trade_date")
        start, end = grp["trade_date"].iloc[0], grp["trade_date"].iloc[-1]
        fx_start, fx_end = _asof(fx, start), _asof(fx, end)
        if fx_start is None or fx_end is None:
            continue
        r_inr = float(grp["adjusted_close"].iloc[-1] / grp["adjusted_close"].iloc[0] - 1.0)
        unhedged = (1.0 + r_inr) * (fx_start / fx_end) - 1.0
        rows.append(
            {
                "symbol": sym,
                "sector": grp["sector"].iloc[0],
                "start_date": start.date(),
                "end_date": end.date(),
                "inr_return": r_inr,
                "fx_start": fx_start,
                "fx_end": fx_end,
                "usd_unhedged_return": unhedged,
                "usd_hedged_return": r_inr,
                "hedge_benefit": r_inr - unhedged,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def macro_latest(engine: Engine, code: str) -> pd.Series:
    """Latest-vintage observations of a macro series, indexed by obs_date."""
    df = _read(engine, MACRO_LATEST_SQL, code=code)
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df.set_index("obs_date")["value"].astype(float).dropna()


def nifty_vs_us10y_correlation(engine: Engine, lag_days: int = 1) -> pd.DataFrame:
    """Correlation of daily stock returns with daily changes in the US 10y Treasury yield.

    US markets close after Indian markets, so a same-calendar-day yield change cannot have
    been known to Indian traders. With `lag_days=1` an Indian session on day T is matched
    with the most recent US yield change dated strictly before T, the information available
    at the Indian open. With `lag_days=0` the dates match exactly (contemporaneous).

    Returns:
        One row per symbol plus an equal-weight `UNIVERSE` row: n_obs, correlation and beta
        (return per 1 percentage-point yield change).
    """
    cols = ["symbol", "sector", "n_obs", "correlation", "beta_per_pp"]
    px = adjusted_prices(engine)
    y = macro_latest(engine, "DGS10")
    if px.empty or len(y) < 3:
        return pd.DataFrame(columns=cols)
    dy = y.diff().dropna().rename("dy").reset_index().rename(columns={"obs_date": "us_date"})
    wide = px.pivot_table(index="trade_date", columns="symbol", values="adjusted_close")
    rets = wide.pct_change(fill_method=None).iloc[1:]
    rets["UNIVERSE"] = rets.mean(axis=1)
    left = rets.reset_index().sort_values("trade_date")
    merged = pd.merge_asof(
        left,
        dy.sort_values("us_date"),
        left_on="trade_date",
        right_on="us_date",
        direction="backward",
        allow_exact_matches=lag_days == 0,
    )
    if lag_days == 0:
        merged = merged[merged["trade_date"] == merged["us_date"]]
    sector_of = px.drop_duplicates("symbol").set_index("symbol")["sector"].to_dict()
    rows = []
    for sym in list(rets.columns):
        pair = merged[[sym, "dy"]].dropna()
        if len(pair) < 3:
            continue
        var = float(pair["dy"].var())
        cov = float(pair[sym].cov(pair["dy"]))
        rows.append(
            {
                "symbol": sym,
                "sector": sector_of.get(sym, "All"),
                "n_obs": len(pair),
                "correlation": float(pair[sym].corr(pair["dy"])),
                "beta_per_pp": cov / var if var > 0 else float("nan"),
            }
        )
    return pd.DataFrame(rows, columns=cols)
