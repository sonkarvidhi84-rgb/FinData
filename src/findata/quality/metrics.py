"""Data-quality metrics along the six DAMA dimensions, computed from the warehouse.

Each metric is a pure function over a DataFrame, so it can be unit-tested with hand-computed
fixtures. `compute_all` reads the warehouse once, applies the functions per dataset and grades
each value against the thresholds in the dataset's YAML `quality:` block.

Grading: `pass` if the threshold is met; `warn` if a ratio misses it by at most 2 percentage
points (or an hours metric by at most 10%); `fail` otherwise; `n/a` without a threshold or value.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from findata.calendar import trading_days as calendar_trading_days
from findata.config import load_dataset
from findata.validate.rules import cross_source_alignment

DQ_METRIC_COLUMNS = [
    "run_id",
    "run_ts",
    "dataset",
    "dimension",
    "metric",
    "value",
    "threshold",
    "status",
]
IST = "Asia/Kolkata"
WARN_BAND_PP = 0.02
WARN_BAND_HOURS = 0.10
NSE_CLOSE_IST = "15:30"
FBIL_FIXING_IST = "13:30"
SCHEMA_RULE_PREFIX = "schema"
DUPLICATE_RULE = "duplicate_keys"


# --------------------------------------------------------------------------- grading


def _is_missing(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def status_for(value: float | None, threshold: float | None, higher_is_better: bool = True) -> str:
    """Grade a metric value against its threshold.

    Args:
        value: Metric value (ratios in [0, 1], timeliness in hours).
        threshold: Minimum (ratios) or maximum (hours) acceptable value.
        higher_is_better: False for lag-type metrics where smaller is better.
    """
    if _is_missing(value) or _is_missing(threshold):
        return "n/a"
    assert value is not None and threshold is not None
    if higher_is_better:
        if value >= threshold:
            return "pass"
        return "warn" if value >= threshold - WARN_BAND_PP else "fail"
    if value <= threshold:
        return "pass"
    return "warn" if value <= threshold * (1 + WARN_BAND_HOURS) else "fail"


# --------------------------------------------------------------------------- completeness


def price_completeness(prices: pd.DataFrame, expected_days: Iterable[pd.Timestamp]) -> float:
    """Share of expected (security x trading day) rows that are present.

    A security is expected from its first observed session to the last session in the data,
    so a stock that stops trading mid-period (a suspension or a missing file) counts as
    missing rows. A stock listed mid-period does not.

    Args:
        prices: Frame with `symbol`, `trade_date`.
        expected_days: Exchange sessions; if empty, the union of observed dates is used.
    """
    if prices.empty:
        return float("nan")
    dates = pd.to_datetime(prices["trade_date"]).dt.normalize()
    cal = pd.DatetimeIndex(sorted({pd.Timestamp(d).normalize() for d in expected_days}))
    if cal.empty:
        cal = pd.DatetimeIndex(sorted(dates.unique()))
    hi = dates.max()
    expected = 0
    present = 0
    for _, idx in prices.groupby("symbol").groups.items():
        observed = set(dates.loc[idx])
        first = min(observed)
        window = cal[(cal >= first) & (cal <= hi)]
        expected += len(window)
        present += len(observed.intersection(window))
    return present / expected if expected else float("nan")


def nonnull_ratio(df: pd.DataFrame, cols: list[str]) -> float:
    """Share of non-null cells across `cols` (completeness for non-calendar datasets)."""
    cols = [c for c in cols if c in df.columns]
    if df.empty or not cols:
        return float("nan")
    return float(df[cols].notna().to_numpy().mean())


def panel_completeness(df: pd.DataFrame, date_col: str, entity_col: str) -> float:
    """Share of (entity x date) cells present, over the union of observed dates.

    Used for FX fixings: if USD/INR is published on a date, the other pairs should be too.
    """
    if df.empty:
        return float("nan")
    n_dates = df[date_col].nunique()
    n_entities = df[entity_col].nunique()
    present = len(df.drop_duplicates([date_col, entity_col]))
    return present / (n_dates * n_entities)


# --------------------------------------------------------------------------- timeliness


def timeliness_hours(
    dates: pd.Series, source_ts: pd.Series, local_time: str = NSE_CLOSE_IST
) -> tuple[float, float]:
    """Median and 95th-percentile lag in hours between the scheduled event and publication.

    For bhavcopy the event is the 15:30 IST close; for FBIL the 13:30 IST fixing. `source_ts`
    is when the source published the record (e.g. the HTTP Last-Modified header), so this
    measures the source's own latency, not how late our backfill ran.
    """
    d = pd.to_datetime(dates).dt.normalize()
    ts = pd.to_datetime(source_ts, utc=True)
    hh, mm = (int(x) for x in local_time.split(":"))
    event = (d + pd.Timedelta(hours=hh, minutes=mm)).dt.tz_localize(IST).dt.tz_convert("UTC")
    lag = ((ts - event).dt.total_seconds() / 3600.0).dropna()
    if lag.empty:
        return float("nan"), float("nan")
    return float(lag.median()), float(lag.quantile(0.95))


# --------------------------------------------------------------------------- accuracy


def split_factors(prices: pd.DataFrame, actions: pd.DataFrame | None) -> pd.Series:
    """Cumulative split/bonus factor to put an unadjusted price on today's share basis.

    For each (symbol, trade_date) it multiplies `ratio` (post/pre price) of every split or
    bonus whose ex-date is after the trade date. Yahoo's `Close` is split-adjusted in the same
    way, so `nse_close * factor` should match it.
    """
    out = pd.Series(1.0, index=prices.index)
    if actions is None or actions.empty:
        return out
    acts = actions[actions["action_type"].isin(["split", "bonus"]) & actions["ratio"].notna()]
    if acts.empty:
        return out
    trade = pd.to_datetime(prices["trade_date"])
    for _, act in acts.iterrows():
        mask = (prices["symbol"] == act["symbol"]) & (trade < pd.Timestamp(act["ex_date"]))
        out[mask] = out[mask] * float(act["ratio"])
    return out


def accuracy_share(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    tolerance_pct: float,
    actions: pd.DataFrame | None = None,
) -> float:
    """Share of daily returns where NSE and the second source agree within `tolerance_pct` pp.

    Comparing *returns* rather than price levels makes the check immune to Yahoo restating
    its whole history for corporate actions after our window: a constant scale factor
    cancels out of a return. Alignment and split adjustment come from
    `findata.validate.rules.cross_source_alignment`, the same logic the validation rule uses.
    """
    aligned = cross_source_alignment(primary, secondary, actions)
    if aligned.empty:
        return float("nan")
    diffs: pd.Series = aligned["ret_diff_pct"].dropna()
    if diffs.empty:
        return float("nan")
    return float((diffs <= tolerance_pct).mean())


def basis_mismatch_symbols(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    tolerance_pct: float,
    actions: pd.DataFrame | None = None,
) -> list[str]:
    """Symbols whose median second-source/NSE price ratio is off 1 by more than the tolerance.

    Each one is a single reference-data gap: the vendor restated history for a corporate
    action that our corporate-action window does not contain. It is not a per-day price error.
    """
    aligned = cross_source_alignment(primary, secondary, actions)
    if aligned.empty:
        return []
    scale = aligned.groupby("symbol")["scale"].first()
    return sorted(str(s) for s, v in scale.items() if abs(float(v) - 1) * 100 > tolerance_pct)


# --------------------------------------------------------------------------- consistency


def ohlc_sanity_share(df: pd.DataFrame) -> float:
    """Share of rows with positive prices and low <= open, close <= high."""
    cols = ["open", "high", "low", "close"]
    d = df.dropna(subset=cols)
    if d.empty:
        return float("nan")
    ok = (
        (d[cols] > 0).all(axis=1)
        & (d["low"] <= d[["open", "close"]].min(axis=1))
        & (d["high"] >= d[["open", "close"]].max(axis=1))
    )
    return float(ok.mean())


_NAME_STOPWORDS = {"LTD", "LIMITED", "THE", "CO", "COMPANY", "CORP", "CORPORATION", "OF", "AND"}


def _name_tokens(x: Any) -> set[str]:
    words = re.sub(r"[^A-Z0-9 ]", " ", str(x).upper()).split()
    return {w for w in words if w not in _NAME_STOPWORDS}


def isin_name_agreement(figi: pd.DataFrame, nse: pd.DataFrame, min_jaccard: float = 0.5) -> float:
    """Share of ISINs where OpenFIGI's instrument name matches NSE's company name.

    Tickers cannot be compared: OpenFIGI returns *Bloomberg* tickers (INFO for Infosys, HUVR
    for Hindustan Unilever), which differ from NSE symbols by design. Instead we compare
    names after dropping legal-form words (LTD/LIMITED...). A token-set Jaccard similarity
    >= `min_jaccard` counts as agreement. A mismatch suggests the ISIN resolved to a
    different instrument, which is a real identifier-mapping error.

    Args:
        figi: `isin`, `name` from OpenFIGI.
        nse: `isin`, `name` from NSE.
    """
    if figi.empty or nse.empty:
        return float("nan")
    m = figi[["isin", "name"]].merge(
        nse[["isin", "name"]].drop_duplicates("isin"), on="isin", how="inner", suffixes=("_f", "_n")
    )
    m = m[m["name_f"].notna() & m["name_n"].notna()]
    if m.empty:
        return float("nan")

    def jaccard(a: Any, b: Any) -> float:
        ta, tb = _name_tokens(a), _name_tokens(b)
        return len(ta & tb) / len(ta | tb) if ta | tb else 0.0

    sims = [jaccard(a, b) for a, b in zip(m["name_f"], m["name_n"], strict=True)]
    return sum(x >= min_jaccard for x in sims) / len(sims)


# --------------------------------------------------------------------------- validity/uniqueness


def violation_share_complement(rows: int, violations: int) -> float:
    """1 - violations/rows, the share of records without a given violation."""
    if rows <= 0:
        return float("nan")
    return max(0.0, 1.0 - violations / rows)


# --------------------------------------------------------------------------- warehouse access

_QUERIES: dict[str, str] = {
    "prices": """
        SELECT s.symbol, p.trade_date, p.source, p.isin_observed, p.open, p.high, p.low,
               p.close, p.volume, p.source_ts
        FROM fact_price_daily p JOIN dim_security s ON s.security_id = p.security_id""",
    "actions": """
        SELECT s.symbol, a.ex_date, a.record_date, a.action_type, a.subject, a.ratio, a.amount
        FROM fact_corporate_action a JOIN dim_security s ON s.security_id = a.security_id""",
    "fx": """
        SELECT f.rate_date, b.code AS base_ccy, f.rate, f.units, f.source_ts
        FROM fact_fx_rate f JOIN dim_currency b ON b.currency_id = f.base_currency_id""",
    "macro": """
        SELECT d.source, d.code AS series_code, m.obs_date, m.vintage_date, m.value
        FROM fact_macro_series m JOIN dim_macro_series d ON d.series_id = m.series_id""",
    "securities": "SELECT * FROM dim_security",
    "issuers": "SELECT * FROM dim_issuer",
    "idmap": """
        SELECT s.symbol, i.id_type, i.id_value, i.source, i.valid_from, i.valid_to
        FROM map_security_identifier i JOIN dim_security s ON s.security_id = i.security_id""",
    "anomalies": "SELECT dataset, rule, record_key, symbol, obs_date FROM dq_anomaly",
}

_DATE_COLS = {"trade_date", "ex_date", "record_date", "rate_date", "obs_date", "vintage_date"}


def load_warehouse(engine: Engine) -> dict[str, pd.DataFrame]:
    """Read the frames metrics need. Dates are parsed; source_ts becomes tz-aware UTC."""
    out: dict[str, pd.DataFrame] = {}
    with engine.connect() as conn:
        for name, sql in _QUERIES.items():
            df = pd.read_sql(text(sql), conn)
            for col in df.columns:
                if col in _DATE_COLS:
                    df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
                elif col == "source_ts":
                    df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
            out[name] = df
    return out


def latest_vintage(macro: pd.DataFrame) -> pd.DataFrame:
    """Latest vintage per (source, series, obs_date)."""
    if macro.empty:
        return macro
    return (
        macro.sort_values("vintage_date")
        .drop_duplicates(["source", "series_code", "obs_date"], keep="last")
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------- orchestration


def _threshold(quality: dict[str, float], key: str) -> float | None:
    v = quality.get(key)
    return float(v) if v is not None else None


def _quality_block(dataset: str) -> dict[str, float]:
    try:
        return load_dataset(dataset).quality
    except KeyError:
        return {}


def compute_all(
    engine: Engine,
    run_id: str,
    run_ts: datetime,
    wh: dict[str, pd.DataFrame] | None = None,
    expected_days: Callable[[], list[pd.Timestamp]] = calendar_trading_days,
    staging: Callable[[str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Compute every metric for every dataset; returns rows shaped like `dq_metric`.

    Args:
        engine: Warehouse engine.
        run_id: Identifier of this quality run.
        run_ts: Run timestamp (UTC).
        wh: Pre-loaded warehouse frames (tests); loaded from `engine` if None.
        expected_days: Provider of expected NSE sessions.
        staging: Reader of staged datasets (for OpenFIGI tickers); defaults to
            `findata.store.read_staging`.
    """
    wh = wh if wh is not None else load_warehouse(engine)
    if staging is None:
        from findata.store import read_staging

        staging = read_staging
    rows: list[dict[str, Any]] = []

    def add(
        dataset: str,
        dimension: str,
        metric: str,
        value: float,
        threshold_key: str | None = None,
        higher_is_better: bool = True,
    ) -> None:
        q = _quality_block(dataset)
        thr = _threshold(q, threshold_key) if threshold_key else None
        val = None if _is_missing(value) else float(value)
        rows.append(
            {
                "run_id": run_id,
                "run_ts": run_ts,
                "dataset": dataset,
                "dimension": dimension,
                "metric": metric,
                "value": val,
                "threshold": thr,
                "status": status_for(val, thr, higher_is_better),
            }
        )

    anomalies = wh["anomalies"]

    def violations(dataset: str, schema: bool) -> int:
        if anomalies.empty:
            return 0
        a = anomalies[anomalies["dataset"] == dataset]
        if schema:
            return int(a["rule"].astype(str).str.startswith(SCHEMA_RULE_PREFIX).sum())
        return int((a["rule"] == DUPLICATE_RULE).sum())

    def validity_uniqueness(dataset: str, n_rows: int) -> None:
        add(
            dataset,
            "validity",
            "validity_share",
            violation_share_complement(n_rows, violations(dataset, True)),
            "validity_min",
        )
        add(
            dataset,
            "uniqueness",
            "uniqueness_share",
            violation_share_complement(n_rows, violations(dataset, False)),
            "uniqueness_min",
        )

    prices = wh["prices"]
    nse = prices[prices["source"] == "nse"] if not prices.empty else prices
    yahoo = prices[prices["source"] == "yahoo"] if not prices.empty else prices
    actions = wh["actions"]
    days = expected_days()

    # nse_prices
    add(
        "nse_prices",
        "completeness",
        "row_completeness",
        price_completeness(nse, days),
        "completeness_min",
    )
    if not nse.empty:
        med, p95 = timeliness_hours(nse["trade_date"], nse["source_ts"], NSE_CLOSE_IST)
    else:
        med, p95 = float("nan"), float("nan")
    add("nse_prices", "timeliness", "publish_lag_median_hours", med, "timeliness_max_hours", False)
    add("nse_prices", "timeliness", "publish_lag_p95_hours", p95)
    tol = _quality_block("nse_prices").get("accuracy_tolerance_pct", 0.5)
    acc = accuracy_share(nse, yahoo, float(tol), actions) if not yahoo.empty else float("nan")
    add("nse_prices", "accuracy", "daily_return_agreement_vs_yahoo", acc, "accuracy_min")
    mismatched = basis_mismatch_symbols(nse, yahoo, float(tol), actions) if not yahoo.empty else []
    add("nse_prices", "accuracy", "symbols_with_price_basis_mismatch", float(len(mismatched)))
    add("nse_prices", "consistency", "ohlc_sanity_share", ohlc_sanity_share(nse), "consistency_min")
    validity_uniqueness("nse_prices", len(nse))

    # yahoo_prices
    add(
        "yahoo_prices",
        "completeness",
        "row_completeness",
        price_completeness(yahoo, days),
        "completeness_min",
    )
    add("yahoo_prices", "consistency", "ohlc_sanity_share", ohlc_sanity_share(yahoo))
    validity_uniqueness("yahoo_prices", len(yahoo))

    # corporate actions
    add(
        "nse_corporate_actions",
        "completeness",
        "required_field_completeness",
        nonnull_ratio(actions, ["ex_date", "action_type", "subject"]),
        "completeness_min",
    )
    validity_uniqueness("nse_corporate_actions", len(actions))

    # fx
    fx = wh["fx"]
    add(
        "fx_rates",
        "completeness",
        "pair_date_completeness",
        panel_completeness(fx, "rate_date", "base_ccy"),
        "completeness_min",
    )
    if not fx.empty:
        fmed, fp95 = timeliness_hours(fx["rate_date"], fx["source_ts"], FBIL_FIXING_IST)
    else:
        fmed, fp95 = float("nan"), float("nan")
    add("fx_rates", "timeliness", "publish_lag_median_hours", fmed, "timeliness_max_hours", False)
    add("fx_rates", "timeliness", "publish_lag_p95_hours", fp95)
    validity_uniqueness("fx_rates", len(fx))

    # macro
    macro = latest_vintage(wh["macro"])
    for dataset, source in (("fred_macro", "fred"), ("worldbank_macro", "worldbank")):
        m = macro[macro["source"] == source] if not macro.empty else macro
        add(
            dataset,
            "completeness",
            "value_completeness",
            nonnull_ratio(m, ["value"]),
            "completeness_min",
        )
        n_vintages = (
            wh["macro"][wh["macro"]["source"] == source]["vintage_date"].nunique()
            if not wh["macro"].empty
            else 0
        )
        add(dataset, "consistency", "vintages_stored", float(n_vintages))
        validity_uniqueness(dataset, len(m))

    # securities master
    secs = wh["securities"]
    add(
        "nse_securities",
        "completeness",
        "attribute_completeness",
        nonnull_ratio(secs, ["symbol", "name", "listing_date", "face_value"]),
        "completeness_min",
    )
    validity_uniqueness("nse_securities", len(secs))

    # OpenFIGI
    idmap = wh["idmap"]
    isins = idmap[idmap["id_type"] == "ISIN"] if not idmap.empty else idmap
    figis = idmap[idmap["id_type"] == "FIGI"] if not idmap.empty else idmap
    n_isin_secs = isins["symbol"].nunique() if not isins.empty else 0
    figi_cov = (
        figis["symbol"].nunique() / n_isin_secs if n_isin_secs and not figis.empty else float("nan")
    )
    if n_isin_secs and figis.empty:
        figi_cov = 0.0
    add("openfigi_mapping", "completeness", "securities_with_figi", figi_cov, "completeness_min")
    figi_stage = staging("openfigi_mapping")
    nse_names = (
        isins[["id_value", "symbol"]]
        .rename(columns={"id_value": "isin"})
        .merge(secs[["symbol", "name"]], on="symbol", how="left")
        if not isins.empty and not secs.empty
        else pd.DataFrame(columns=["isin", "name"])
    )
    agree = (
        isin_name_agreement(figi_stage, nse_names)
        if {"isin", "name"}.issubset(figi_stage.columns)
        else float("nan")
    )
    add("openfigi_mapping", "consistency", "isin_name_agreement", agree, "consistency_min")
    validity_uniqueness("openfigi_mapping", len(figi_stage) if not figi_stage.empty else len(figis))

    # GLEIF
    issuers = wh["issuers"]
    lei_cov = (
        float(secs["issuer_id"].notna().mean())
        if not secs.empty and "issuer_id" in secs
        else float("nan")
    )
    add("gleif_lei", "completeness", "securities_with_lei", lei_cov, "completeness_min")
    validity_uniqueness("gleif_lei", len(issuers))

    df = pd.DataFrame(rows, columns=DQ_METRIC_COLUMNS)
    df["value"] = df["value"].astype("float64")
    return df
