"""Validation rule engine: pure functions that turn financial red flags into `Anomaly` records.

Every rule has the signature `rule(df, ctx) -> list[Anomaly]`, depends on nothing but its
inputs, and never raises on bad data. A rule that can't run (a missing column, no context data)
returns an empty list. Record keys follow `SYMBOL|YYYY-MM-DD` (or `SERIES|date`, `CCY|date`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

import numpy as np
import pandas as pd

from findata.validate.anomaly import Anomaly, RuleContext

Rule = Callable[[pd.DataFrame, RuleContext], list[Anomaly]]

PRICE_ACTIONS = ("split", "bonus")


def _d(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _ts(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value)


def _has(df: pd.DataFrame | None, cols: Sequence[str]) -> bool:
    return df is not None and not df.empty and all(c in df.columns for c in cols)


# --------------------------------------------------------------------------- price sanity


def ohlc_sanity(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag bars whose open/high/low/close are internally inconsistent.

    Financial problem: a high below the close, a low above the open, or a non-positive price
    can't happen in a real session. They come from feed corruption, a column swap, or a
    mis-keyed manual correction, and they silently poison returns, volatility and VaR.

    Checks: all prices > 0, low <= min(open, close), high >= max(open, close), low <= high.
    Rows with any missing OHLC value are skipped (completeness is measured elsewhere).
    """
    cols = ["symbol", "trade_date", "open", "high", "low", "close"]
    if not _has(df, cols):
        return []
    sev = ctx.severity("high")
    bars = df.dropna(subset=["open", "high", "low", "close"])
    eps = 1e-9
    o, h, lo, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    nonpos = (o <= 0) | (h <= 0) | (lo <= 0) | (c <= 0)
    low_bad = lo > np.minimum(o, c) + eps
    high_bad = h < np.maximum(o, c) - eps
    inverted = lo > h + eps
    out: list[Anomaly] = []
    for i, (_, row) in enumerate(bars.iterrows()):
        problems = []
        if nonpos[i]:
            problems.append("non-positive price")
        if low_bad[i]:
            problems.append("low above open/close")
        if high_bad[i]:
            problems.append("high below open/close")
        if inverted[i]:
            problems.append("low above high")
        if problems:
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="ohlc_sanity",
                    severity=sev,
                    record_key=f"{row['symbol']}|{_d(row['trade_date'])}",
                    detail=(
                        f"{', '.join(problems)}: O={row['open']} H={row['high']} "
                        f"L={row['low']} C={row['close']}"
                    ),
                    symbol=str(row["symbol"]),
                    obs_date=_d(row["trade_date"]),
                    value=float(row["close"]),
                )
            )
    return out


def missing_trading_days(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag expected NSE sessions with no price row for a security.

    Financial problem: a missing day makes the next return a two-day return. It understates
    the number of observations and biases volatility and drawdown figures. Typical causes are
    a failed download, a suspended stock, or a holiday calendar that's wrong.

    Only gaps between a symbol's first and last observed dates are checked, so a new listing
    or a dataset that starts later isn't penalised.
    """
    if not _has(df, ["symbol", "trade_date"]) or not ctx.trading_days:
        return []
    sev = ctx.severity("medium")
    expected = pd.DatetimeIndex(sorted({pd.Timestamp(d) for d in ctx.trading_days}))
    out: list[Anomaly] = []
    for symbol, grp in df.groupby("symbol", sort=True):
        seen = pd.DatetimeIndex(pd.to_datetime(grp["trade_date"]).unique())
        window = expected[(expected >= seen.min()) & (expected <= seen.max())]
        for day in window.difference(seen):
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="missing_trading_days",
                    severity=sev,
                    record_key=f"{symbol}|{_d(day)}",
                    detail=f"No {symbol} row for expected NSE session {_d(day)}",
                    symbol=str(symbol),
                    obs_date=_d(day),
                )
            )
    return out


def duplicate_keys(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag natural keys that occur more than once.

    Financial problem: two rows for the same (ISIN, date) double-count volume and market value
    in aggregates, and make "the" price ambiguous. It usually means a re-run appended rather
    than upserted, or a vendor re-published a correction alongside the original.

    Params:
        key: list of natural-key columns (set by the engine from the dataset YAML).
    """
    key = [str(k) for k in ctx.params.get("key", [])]
    if not key or not _has(df, key):
        return []
    sev = ctx.severity("high")
    dup = df[df.duplicated(subset=key, keep=False)]
    out: list[Anomaly] = []
    for values, grp in dup.groupby(key, sort=True, dropna=False):
        vals = values if isinstance(values, tuple) else (values,)
        parts = [_d(v) if isinstance(v, pd.Timestamp) else str(v) for v in vals]
        row = grp.iloc[0]
        date_col = next((c for c in key if isinstance(row[c], pd.Timestamp)), None)
        sym_col = next((c for c in ("symbol", "series_id", "base_ccy", "isin") if c in key), None)
        out.append(
            Anomaly(
                dataset=ctx.dataset,
                rule="duplicate_keys",
                severity=sev,
                record_key="|".join(parts),
                detail=f"Natural key {dict(zip(key, parts, strict=True))} appears {len(grp)} times",
                symbol=str(row[sym_col]) if sym_col else None,
                obs_date=_d(row[date_col]) if date_col else None,
                value=float(len(grp)),
            )
        )
    return out


def stale_price(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag a close that stays identical for N+ consecutive sessions while volume is traded.

    Financial problem: a liquid stock that trades shares but never changes its closing price is
    almost always a stuck feed (yesterday's file re-served) rather than a real market. Stale
    prices understate volatility and hide losses in a mark-to-market.

    Params:
        min_sessions: run length that triggers a flag (default 5).
    """
    if not _has(df, ["symbol", "trade_date", "close", "volume"]):
        return []
    n = int(ctx.params.get("min_sessions", 5))
    sev = ctx.severity("medium")
    out: list[Anomaly] = []
    for symbol, grp in df.sort_values("trade_date").groupby("symbol", sort=True):
        g = grp.reset_index(drop=True)
        traded = g["volume"].fillna(0) > 0
        same = g["close"].eq(g["close"].shift()) & traded
        run_id = (~same).cumsum()
        for _, run in g.groupby(run_id):
            if len(run) >= n and bool((run["volume"].fillna(0) > 0).all()):
                last = run.iloc[-1]
                out.append(
                    Anomaly(
                        dataset=ctx.dataset,
                        rule="stale_price",
                        severity=sev,
                        record_key=f"{symbol}|{_d(last['trade_date'])}",
                        detail=(
                            f"Close {last['close']} unchanged for {len(run)} sessions "
                            f"({_d(run['trade_date'].iloc[0])} to {_d(last['trade_date'])}) "
                            "with non-zero volume"
                        ),
                        symbol=str(symbol),
                        obs_date=_d(last["trade_date"]),
                        value=float(len(run)),
                    )
                )
    return out


# --------------------------------------------------------------------------- corporate actions


def _actions_for(ctx: RuleContext, symbol: str) -> pd.DataFrame:
    ca = ctx.corporate_actions
    if not _has(ca, ["symbol", "ex_date", "action_type"]):
        return pd.DataFrame(columns=["symbol", "ex_date", "action_type", "ratio", "amount"])
    assert ca is not None
    return ca[ca["symbol"] == symbol]


def unexplained_jump(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag close-to-close moves above a threshold with no corporate action nearby.

    Financial problem: a 1:10 split makes the price fall about 90% overnight with no change in
    value. If the split isn't in our corporate-action table, an unadjusted series shows a crash.
    A large move with no action nearby is either genuine news, which an analyst should confirm,
    or a bad tick, which must be corrected before it reaches returns.

    The return is computed from our own previous row, not NSE's published `prev_close`, which
    NSE already adjusts for corporate actions. A split, bonus or rights issue within
    ±window_days of the move explains it. A dividend explains it only if the dividend is at
    least half the size of the move.

    Params:
        threshold: absolute return that triggers a flag (e.g. 0.15).
        window_days: calendar days around the ex-date that count as "nearby".
    """
    if not _has(df, ["symbol", "trade_date", "close"]):
        return []
    thr = float(ctx.params.get("threshold", 0.2))
    win = pd.Timedelta(days=int(ctx.params.get("window_days", 2)))
    sev = ctx.severity("high")
    out: list[Anomaly] = []
    for symbol, grp in df.sort_values("trade_date").groupby("symbol", sort=True):
        g = grp.reset_index(drop=True)
        prev = g["close"].shift()
        ret = g["close"] / prev - 1.0
        actions = _actions_for(ctx, str(symbol))
        for i in ret.index[ret.abs() > thr]:
            day = _ts(g.at[i, "trade_date"])
            near = actions[(pd.to_datetime(actions["ex_date"]) - day).abs() <= win]
            r = float(ret.iloc[i])
            explained = False
            for _, act in near.iterrows():
                if act["action_type"] in ("split", "bonus", "rights"):
                    explained = True
                elif act["action_type"] == "dividend":
                    amount = act.get("amount")
                    if amount is not None and not pd.isna(amount):
                        explained |= float(amount) / float(prev.iloc[i]) >= abs(r) / 2
            if explained:
                continue
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="unexplained_jump",
                    severity=sev,
                    record_key=f"{symbol}|{_d(day)}",
                    detail=(
                        f"Close moved {r:+.1%} ({prev.iloc[i]} -> {g.at[i, 'close']}) with no "
                        f"split/bonus/rights within ±{win.days} days"
                    ),
                    symbol=str(symbol),
                    obs_date=_d(day),
                    value=r,
                )
            )
    return out


def split_adjustment_check(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Check that the price ratio across a split/bonus ex-date matches the declared ratio.

    Financial problem: if a 1:10 split is declared but the price only halves, then either the
    declared ratio is wrong (and every adjusted series built from it is wrong), or the ex-date
    is wrong. A declared split with no price data around the ex-date can't be verified at all,
    so it's also flagged.

    Observed ratio = close on the first session >= ex_date divided by close on the last session
    before it. Several split/bonus events on the same ex-date are multiplied together.

    Params:
        tolerance: allowed relative error vs the declared ratio (e.g. 0.15 absorbs a normal
            day's market move).
    """
    ca = ctx.corporate_actions
    if not _has(ca, ["symbol", "ex_date", "action_type", "ratio"]) or not _has(
        df, ["symbol", "trade_date", "close"]
    ):
        return []
    assert ca is not None
    tol = float(ctx.params.get("tolerance", 0.15))
    sev = ctx.severity("high")
    events = ca[ca["action_type"].isin(PRICE_ACTIONS) & ca["ratio"].notna()]
    events = events[events["symbol"].isin(df["symbol"].unique())]
    out: list[Anomaly] = []
    for (symbol, ex), grp in events.groupby(["symbol", "ex_date"], sort=True):
        ex = _ts(ex)
        declared = float(np.prod(grp["ratio"].astype(float)))
        px = df[df["symbol"] == symbol].sort_values("trade_date")
        dates = pd.to_datetime(px["trade_date"])
        if dates.empty or ex < dates.min() or ex > dates.max():
            continue  # outside the price history we hold; nothing to verify
        before = px[dates < ex]
        after = px[dates >= ex]
        key = f"{symbol}|{_d(ex)}"
        if before.empty or after.empty:
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="split_adjustment_check",
                    severity=sev,
                    record_key=key,
                    detail=f"Declared ratio {declared:.4g} on {_d(ex)} but no prices on both sides",
                    symbol=str(symbol),
                    obs_date=_d(ex),
                )
            )
            continue
        observed = float(after["close"].iloc[0]) / float(before["close"].iloc[-1])
        err = abs(observed / declared - 1.0)
        if err > tol:
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="split_adjustment_check",
                    severity=sev,
                    record_key=key,
                    detail=(
                        f"Declared price ratio {declared:.4g} but observed {observed:.4g} "
                        f"({err:.0%} off, tolerance {tol:.0%})"
                    ),
                    symbol=str(symbol),
                    obs_date=_d(ex),
                    value=observed,
                )
            )
    return out


def split_factor_series(
    trade_dates: pd.Series, ex_dates: Sequence[Any], ratios: Sequence[float]
) -> pd.Series:
    """Cumulative split/bonus factor that puts an unadjusted price on today's share basis.

    For each trade date, the factor is the product of `ratio` over every event with
    `ex_date > trade_date`. Multiplying an unadjusted close by it gives the split-adjusted
    close, the convention Yahoo uses for its `Close` column. Without this step, a 1:10 split
    looks like a 900% cross-source "error" on every pre-split day.

    Args:
        trade_dates: Dates to compute factors for.
        ex_dates: Ex-dates of split/bonus events.
        ratios: Post/pre price ratio per event (0.1 for a 1:10 split, 0.5 for a 1:1 bonus).

    Returns:
        Float series aligned to `trade_dates`.
    """
    dates = pd.to_datetime(trade_dates)
    factor = pd.Series(1.0, index=trade_dates.index, dtype="float64")
    for ex, ratio in zip(ex_dates, ratios, strict=True):
        factor = factor.where(~(dates < pd.Timestamp(ex)), factor * float(ratio))
    return factor


def cross_source_alignment(
    primary: pd.DataFrame, secondary: pd.DataFrame, corporate_actions: pd.DataFrame | None
) -> pd.DataFrame:
    """Align NSE and second-source closes so they can be compared fairly.

    Two corrections are needed before two vendors can be compared:

    1. **In-window splits/bonuses.** NSE closes are unadjusted. They are multiplied by
       `split_factor_series` from our own corporate actions.
    2. **Out-of-window restatements.** Yahoo restates its whole history for corporate actions
       that happen *after* our window: later bonuses, splits and demergers. In the 2024 data
       Yahoo's close was 0.5x NSE for HDFCBANK and RELIANCE (bonuses that went ex later), 0.2x
       for KOTAKBANK and 0.9629x for ITC (the 2025 ITC Hotels demerger). These are constant
       scale factors over the window. They are estimated as `scale` (the median ratio), but
       the comparison itself runs on **returns**, which are scale-invariant.

    Returns are taken between consecutive dates *present in both sources*, so a day one
    vendor skips (Yahoo has none of NSE's Saturday special sessions) cannot create a mismatch.

    Returns:
        One row per (symbol, trade_date) with `close` (adjusted NSE), `close_2`, `scale`,
        `ret`, `ret_2` and `ret_diff_pct` (absolute return difference in percentage points;
        NaN on each symbol's first common date).
    """
    cols = ["symbol", "trade_date", "close"]
    out_cols = [*cols, "close_2", "split_factor", "scale", "ret", "ret_2", "ret_diff_pct"]
    left = primary[cols].assign(trade_date=pd.to_datetime(primary["trade_date"]))
    right = secondary[cols].rename(columns={"close": "close_2"})
    right = right.assign(trade_date=pd.to_datetime(right["trade_date"]))
    merged = (
        left.merge(right, on=["symbol", "trade_date"], how="inner")
        .dropna(subset=["close", "close_2"])
        .sort_values(["symbol", "trade_date"])
    )
    has_ca = _has(corporate_actions, ["symbol", "ex_date", "action_type", "ratio"])
    frames: list[pd.DataFrame] = []
    for symbol, grp in merged.groupby("symbol", sort=True):
        grp = grp.copy()
        if has_ca:
            assert corporate_actions is not None
            acts = corporate_actions[corporate_actions["symbol"] == symbol]
            acts = acts[acts["action_type"].isin(PRICE_ACTIONS) & acts["ratio"].notna()]
            ex_dates, ratios = list(acts["ex_date"]), [float(r) for r in acts["ratio"]]
        else:
            ex_dates, ratios = [], []
        factor = split_factor_series(grp["trade_date"], ex_dates, ratios)
        grp["close"] = grp["close"] * factor
        grp["split_factor"] = factor
        grp["scale"] = float((grp["close_2"] / grp["close"]).median())
        grp["ret"] = grp["close"].pct_change()
        grp["ret_2"] = grp["close_2"].pct_change()
        grp["ret_diff_pct"] = (grp["ret"] - grp["ret_2"]).abs() * 100.0
        frames.append(grp)
    if not frames:
        return pd.DataFrame(columns=out_cols)
    return pd.concat(frames, ignore_index=True)[out_cols]


def cross_source_price_diff(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag days where NSE and the second source (Yahoo) disagree on the day's price move.

    Financial problem: two independent vendors rarely disagree about an official close. A
    disagreement means one of them is wrong: a bad or stale tick, a wrong-security mapping,
    or a corporate action one side missed. This is how *accuracy* is measured when nobody
    hands you the truth.

    The comparison is on close-to-close returns over common dates, after in-window split
    adjustment (see `cross_source_alignment`). Price levels are not compared, because Yahoo
    restates history for later corporate actions. A missed split therefore surfaces on its
    ex-date, and a bad tick on its own day (and usually the reversal day after).

    A low-severity `cross_source_basis` anomaly is also emitted once per symbol whose level
    scale differs from 1. It is not an error, but anyone reconciling levels needs to know.

    Params:
        threshold_pct: Allowed absolute return difference, in percentage points (e.g. 0.5).
    """
    sec = ctx.secondary
    if not _has(sec, ["symbol", "trade_date", "close"]) or not _has(
        df, ["symbol", "trade_date", "close"]
    ):
        return []
    assert sec is not None
    thr = float(ctx.params.get("threshold_pct", 0.5))
    sev = ctx.severity("medium")
    aligned = cross_source_alignment(df, sec, ctx.corporate_actions)
    out: list[Anomaly] = []
    for symbol, grp in aligned.groupby("symbol", sort=True):
        scale = float(grp["scale"].iloc[0])
        if abs(scale - 1.0) * 100.0 > thr:
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="cross_source_basis",
                    severity="low",
                    record_key=f"{symbol}|basis",
                    detail=(
                        f"Yahoo close is {scale:.4f}x the split-adjusted NSE close across the "
                        "window: Yahoo restated history for a corporate action outside it. "
                        "Levels are not comparable; returns are."
                    ),
                    symbol=str(symbol),
                    obs_date=_d(grp["trade_date"].iloc[0]),
                    value=scale,
                )
            )
        flagged = grp[grp["ret_diff_pct"] > thr]
        for rec in flagged.to_dict("records"):
            day = rec["trade_date"]
            f = float(rec["split_factor"])
            diff = float(rec["ret_diff_pct"])
            note = f", NSE x split factor {f:.4g}" if f != 1.0 else ""
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="cross_source_price_diff",
                    severity=sev,
                    record_key=f"{symbol}|{_d(day)}",
                    detail=(
                        f"Daily return NSE {float(rec['ret']) * 100:+.2f}% vs Yahoo "
                        f"{float(rec['ret_2']) * 100:+.2f}%: {diff:.2f}pp apart "
                        f"(tolerance {thr}pp{note}; NSE close {float(rec['close']):.2f}, "
                        f"Yahoo {float(rec['close_2']):.2f})"
                    ),
                    symbol=str(symbol),
                    obs_date=_d(day),
                    value=diff,
                )
            )
    return out


def identifier_conflict(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag a ticker mapped to two ISINs (or an ISIN to two tickers) on the same date.

    Financial problem: identifiers are how positions, prices and corporate actions get joined.
    A ticker pointing at two ISINs on one day means a join can attach the wrong price to a
    holding. That's a classic reconciliation break.

    A clean change across dates is legitimate. NSE issues a new ISIN after a face-value split
    (NESTLEIND, Jan 2024), for instance. That isn't a conflict, but it's reported as a
    low-severity `identifier_change` so the identifier map can open a new validity window.
    """
    if not _has(df, ["symbol", "isin", "trade_date"]):
        return []
    sev = ctx.severity("high")
    out: list[Anomaly] = []
    obs = df.dropna(subset=["symbol", "isin"])
    for a, b, label in (("symbol", "isin", "ISINs"), ("isin", "symbol", "symbols")):
        counts = obs.groupby([a, "trade_date"])[b].nunique()
        for idx, n in counts[counts > 1].items():
            ident, day = cast(tuple[Any, Any], idx)
            values = sorted(obs[(obs[a] == ident) & (obs["trade_date"] == day)][b].astype(str))
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="identifier_conflict",
                    severity=sev,
                    record_key=f"{ident}|{_d(day)}",
                    detail=f"{a} {ident} maps to {n} {label} on {_d(day)}: {', '.join(values)}",
                    symbol=str(ident) if a == "symbol" else values[0],
                    obs_date=_d(day),
                    value=float(n),
                )
            )
    one_per_day = obs.drop_duplicates(subset=["symbol", "trade_date"]).sort_values("trade_date")
    for symbol, grp0 in one_per_day.groupby("symbol", sort=True):
        grp = grp0.reset_index(drop=True)
        changed = grp["isin"].ne(grp["isin"].shift()) & grp["isin"].shift().notna()
        for pos in np.flatnonzero(changed.to_numpy()):
            i = int(pos)
            old = grp["isin"].iloc[i - 1]
            day = grp.at[i, "trade_date"]
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="identifier_change",
                    severity="low",
                    record_key=f"{symbol}|{_d(day)}",
                    detail=f"{symbol} ISIN changed {old} -> {grp.at[i, 'isin']} from {_d(day)}",
                    symbol=str(symbol),
                    obs_date=_d(day),
                )
            )
    return out


# --------------------------------------------------------------------------- fx & macro


def fx_rate_outlier(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag daily FX moves that are extreme relative to their recent volatility.

    Financial problem: INR reference rates move a few tenths of a percent per day. A day that is
    4+ standard deviations out is usually a units error (JPY per 1 vs per 100), a decimal shift,
    or a mis-dated fixing, and it propagates into every INR-converted valuation.

    z = (log-change - rolling mean) / rolling std, computed from the *previous* `window`
    changes so the outlier cannot hide by inflating its own std.

    Params:
        window: rolling window in observations (default 20).
        z_threshold: absolute z that triggers a flag (default 4).
    """
    if not _has(df, ["rate_date", "base_ccy", "rate"]):
        return []
    window = int(ctx.params.get("window", 20))
    zthr = float(ctx.params.get("z_threshold", 4.0))
    sev = ctx.severity("medium")
    out: list[Anomaly] = []
    for ccy, grp in df.sort_values("rate_date").groupby("base_ccy", sort=True):
        g = grp.reset_index(drop=True)
        units = g["units"] if "units" in g.columns else pd.Series(1.0, index=g.index)
        per_unit = g["rate"].astype(float) / units.fillna(1.0).astype(float)
        r = pd.Series(np.log(per_unit.to_numpy()), index=g.index).diff()
        hist = r.shift()
        mu = hist.rolling(window, min_periods=max(5, window // 2)).mean()
        sd = hist.rolling(window, min_periods=max(5, window // 2)).std()
        z = (r - mu) / sd.replace(0.0, np.nan)
        for i in z.index[z.abs() > zthr]:
            day = g.at[i, "rate_date"]
            out.append(
                Anomaly(
                    dataset=ctx.dataset,
                    rule="fx_rate_outlier",
                    severity=sev,
                    record_key=f"{ccy}|{_d(day)}",
                    detail=(
                        f"{ccy}/INR moved {r.loc[i]:+.2%} on {_d(day)} "
                        f"(z={z.loc[i]:.1f} vs {window}-day history)"
                    ),
                    symbol=str(ccy),
                    obs_date=_d(day),
                    value=float(z.loc[i]),
                )
            )
    return out


def macro_revision_detected(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]:
    """Flag macro observations whose value changed since the previously loaded vintage.

    Financial problem: GDP, CPI and payrolls are revised after first release. A backtest that
    uses today's revised number for a date when only the first print was known has look-ahead
    bias. Revisions are stored as new vintages, never overwritten, and each one is surfaced here.

    `df` is the latest vintage; `ctx.previous` the prior vintage (same columns).
    """
    prev = ctx.previous
    cols = ["series_id", "obs_date", "value"]
    if not _has(prev, cols) or not _has(df, cols):
        return []
    assert prev is not None
    sev = ctx.severity("low")
    m = df[cols].merge(prev[cols], on=["series_id", "obs_date"], suffixes=("", "_prev"))
    a, b = m["value"], m["value_prev"]
    changed = (a.isna() != b.isna()) | (
        a.notna() & b.notna() & ((a - b).abs() > 1e-9 * np.maximum(1.0, b.abs()))
    )
    out: list[Anomaly] = []
    for _, row in m[changed].iterrows():
        out.append(
            Anomaly(
                dataset=ctx.dataset,
                rule="macro_revision_detected",
                severity=sev,
                record_key=f"{row['series_id']}|{_d(row['obs_date'])}",
                detail=(
                    f"{row['series_id']} {_d(row['obs_date'])} revised "
                    f"{row['value_prev']} -> {row['value']}"
                ),
                symbol=str(row["series_id"]),
                obs_date=_d(row["obs_date"]),
                value=None if pd.isna(row["value"]) else float(row["value"]),
            )
        )
    return out


RULES: dict[str, Rule] = {
    "ohlc_sanity": ohlc_sanity,
    "missing_trading_days": missing_trading_days,
    "duplicate_keys": duplicate_keys,
    "stale_price": stale_price,
    "unexplained_jump": unexplained_jump,
    "split_adjustment_check": split_adjustment_check,
    "cross_source_price_diff": cross_source_price_diff,
    "identifier_conflict": identifier_conflict,
    "fx_rate_outlier": fx_rate_outlier,
    "macro_revision_detected": macro_revision_detected,
}
