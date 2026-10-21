"""Each rule has a passing (clean) fixture and a failing (dirty) fixture."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from findata.validate.anomaly import RuleContext
from findata.validate.rules import (
    RULES,
    cross_source_price_diff,
    duplicate_keys,
    fx_rate_outlier,
    identifier_conflict,
    macro_revision_detected,
    missing_trading_days,
    ohlc_sanity,
    split_adjustment_check,
    split_factor_series,
    stale_price,
    unexplained_jump,
)

DATES = list(pd.bdate_range("2024-01-01", periods=10))


def prices(
    closes: list[float], symbol: str = "AAA", isin: str = "INE000A01011", **kw: Any
) -> pd.DataFrame:
    n = len(closes)
    df = pd.DataFrame(
        {
            "trade_date": DATES[:n],
            "symbol": symbol,
            "isin": isin,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )
    for k, v in kw.items():
        df[k] = v
    return df


def ctx(**kw: Any) -> RuleContext:
    return RuleContext(dataset="nse_prices", **kw)


def actions(rows: list[tuple[str, str, str, float | None, float | None]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["symbol", "ex_date", "action_type", "ratio", "amount"]
    ).assign(ex_date=lambda d: pd.to_datetime(d["ex_date"]))


# ---------------------------------------------------------------- ohlc_sanity


def test_ohlc_sanity_clean() -> None:
    assert ohlc_sanity(prices([100, 101, 102]), ctx()) == []


def test_ohlc_sanity_flags_low_above_close_and_nonpositive() -> None:
    df = prices([100, 101, 102])
    df.loc[1, "low"] = 105.0
    df.loc[2, "open"] = 0.0
    out = ohlc_sanity(df, ctx(params={"severity": "high"}))
    assert [a.record_key for a in out] == ["AAA|2024-01-02", "AAA|2024-01-03"]
    assert "low above open/close" in out[0].detail
    assert "non-positive" in out[1].detail
    assert all(a.severity == "high" for a in out)


def test_ohlc_sanity_flags_high_below_and_inverted() -> None:
    df = prices([100])
    df.loc[0, ["open", "high", "low", "close"]] = [100.0, 90.0, 95.0, 100.0]
    out = ohlc_sanity(df, ctx())
    assert "high below open/close" in out[0].detail and "low above high" in out[0].detail


def test_ohlc_sanity_skips_missing_and_missing_columns() -> None:
    df = prices([100])
    df.loc[0, "high"] = np.nan
    assert ohlc_sanity(df, ctx()) == []
    assert ohlc_sanity(pd.DataFrame({"x": [1]}), ctx()) == []


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.tuples(st.floats(0.01, 1e5), st.floats(0.01, 1e5), st.floats(0, 0.5), st.floats(0, 0.5)),
        min_size=1,
        max_size=20,
    )
)
def test_property_valid_ohlc_never_flagged(bars: list[tuple[float, float, float, float]]) -> None:
    o = [b[0] for b in bars]
    c = [b[1] for b in bars]
    hi = [max(a, b) * (1 + u) for a, b, u, _ in bars]
    lo = [min(a, b) * (1 - d) for a, b, _, d in bars]
    df = pd.DataFrame(
        {
            "symbol": "P",
            "trade_date": pd.bdate_range("2024-01-01", periods=len(bars)),
            "open": o,
            "high": hi,
            "low": lo,
            "close": c,
        }
    )
    assert ohlc_sanity(df, ctx()) == []


# ---------------------------------------------------------------- missing_trading_days


def test_missing_trading_days_clean() -> None:
    assert missing_trading_days(prices([1, 2, 3]), ctx(trading_days=DATES)) == []


def test_missing_trading_days_flags_gap_only_inside_history() -> None:
    df = prices([1, 2, 3, 4]).drop(index=1)  # drop 2024-01-02
    out = missing_trading_days(df, ctx(trading_days=DATES))
    assert [a.record_key for a in out] == ["AAA|2024-01-02"]


def test_missing_trading_days_without_calendar() -> None:
    assert missing_trading_days(prices([1, 2]), ctx()) == []


# ---------------------------------------------------------------- duplicate_keys


def test_duplicate_keys_clean() -> None:
    assert duplicate_keys(prices([1, 2]), ctx(params={"key": ["isin", "trade_date"]})) == []


def test_duplicate_keys_flags_repeat() -> None:
    df = prices([1, 2])
    df = pd.concat([df, df.iloc[[1]]], ignore_index=True)
    out = duplicate_keys(df, ctx(params={"key": ["isin", "trade_date"]}))
    assert len(out) == 1
    assert out[0].record_key == "INE000A01011|2024-01-02"
    assert out[0].value == 2.0 and out[0].obs_date == "2024-01-02"


def test_duplicate_keys_needs_key() -> None:
    assert duplicate_keys(prices([1, 1]), ctx()) == []


# ---------------------------------------------------------------- stale_price


def test_stale_price_clean() -> None:
    assert stale_price(prices([1, 2, 3, 4, 5, 6]), ctx(params={"min_sessions": 5})) == []


def test_stale_price_flags_run_with_volume() -> None:
    out = stale_price(prices([1, 2, 2, 2, 2, 2, 3]), ctx(params={"min_sessions": 5}))
    assert len(out) == 1 and out[0].record_key == "AAA|2024-01-08" and out[0].value == 5


def test_stale_price_ignores_zero_volume_run() -> None:
    df = prices([2, 2, 2, 2, 2])
    df["volume"] = 0.0
    assert stale_price(df, ctx(params={"min_sessions": 5})) == []


# ---------------------------------------------------------------- unexplained_jump

JUMP = {"threshold": 0.15, "window_days": 2}


def test_unexplained_jump_clean_small_moves() -> None:
    assert unexplained_jump(prices([100, 105, 101]), ctx(params=JUMP)) == []


def test_unexplained_jump_flags_crash_without_action() -> None:
    out = unexplained_jump(prices([100, 101, 10]), ctx(params=JUMP))
    assert len(out) == 1 and out[0].record_key == "AAA|2024-01-03"
    assert out[0].value == pytest.approx(10 / 101 - 1)


def test_unexplained_jump_explained_by_split() -> None:
    ca = actions([("AAA", "2024-01-03", "split", 0.1, None)])
    assert unexplained_jump(prices([100, 101, 10]), ctx(params=JUMP, corporate_actions=ca)) == []


def test_unexplained_jump_dividend_only_explains_if_large() -> None:
    small = actions([("AAA", "2024-01-03", "dividend", None, 1.0)])
    big = actions([("AAA", "2024-01-03", "dividend", None, 30.0)])
    df = prices([100, 100, 80])
    assert len(unexplained_jump(df, ctx(params=JUMP, corporate_actions=small))) == 1
    assert unexplained_jump(df, ctx(params=JUMP, corporate_actions=big)) == []


# ---------------------------------------------------------------- split_adjustment_check


def test_split_check_clean() -> None:
    ca = actions([("AAA", "2024-01-03", "split", 0.1, None)])
    out = split_adjustment_check(prices([1000, 1010, 102]), ctx(corporate_actions=ca))
    assert out == []


def test_split_check_flags_wrong_ratio() -> None:
    ca = actions([("AAA", "2024-01-03", "split", 0.1, None)])
    out = split_adjustment_check(
        prices([1000, 1000, 500]), ctx(corporate_actions=ca, params={"tolerance": 0.15})
    )
    assert len(out) == 1 and out[0].value == pytest.approx(0.5)


def test_split_check_flags_no_prices_on_one_side() -> None:
    ca = actions([("AAA", "2024-01-01", "bonus", 0.5, None)])
    out = split_adjustment_check(prices([100, 50]), ctx(corporate_actions=ca))
    assert len(out) == 1 and "no prices on both sides" in out[0].detail


def test_split_check_ignores_events_outside_history_and_without_ca() -> None:
    ca = actions([("AAA", "2025-01-01", "split", 0.1, None)])
    assert split_adjustment_check(prices([1, 2]), ctx(corporate_actions=ca)) == []
    assert split_adjustment_check(prices([1, 2]), ctx()) == []


# ---------------------------------------------------------------- split_factor_series


def test_split_factor_series_basic() -> None:
    dates = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-05", "2024-01-10"]))
    f = split_factor_series(dates, [pd.Timestamp("2024-01-05")], [0.1])
    assert f.tolist() == [0.1, 1.0, 1.0]


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(0, 60), min_size=1, max_size=30),
    st.lists(st.tuples(st.integers(0, 60), st.floats(0.05, 1.0)), max_size=4),
)
def test_property_split_factor_is_product_of_later_events(
    offsets: list[int], events: list[tuple[int, float]]
) -> None:
    base = pd.Timestamp("2024-01-01")
    dates = pd.Series([base + pd.Timedelta(days=o) for o in offsets])
    exs = [base + pd.Timedelta(days=e) for e, _ in events]
    ratios = [r for _, r in events]
    f = split_factor_series(dates, exs, ratios)
    for d, val in zip(dates, f, strict=True):
        expected = float(np.prod([r for ex, r in zip(exs, ratios, strict=True) if ex > d]))
        assert val == pytest.approx(expected)
    # after the last event nothing is adjusted
    if exs:
        assert all(v == 1.0 for d, v in zip(dates, f, strict=True) if d >= max(exs))


# ---------------------------------------------------------------- cross_source_price_diff


def test_cross_source_clean_with_split_adjustment() -> None:
    nse = prices([1000, 100, 101])
    yahoo = prices([100, 100, 101])
    ca = actions([("AAA", "2024-01-02", "split", 0.1, None)])
    c = ctx(params={"threshold_pct": 0.5}, secondary=yahoo, corporate_actions=ca)
    assert cross_source_price_diff(nse, c) == []


def test_cross_source_flags_missed_split_and_bad_tick() -> None:
    nse = prices([1000, 100, 110])
    yahoo = prices([100, 100, 101])
    out = cross_source_price_diff(nse, ctx(params={"threshold_pct": 0.5}, secondary=yahoo))
    assert [a.record_key for a in out] == ["AAA|2024-01-01", "AAA|2024-01-03"]
    assert out[0].value == pytest.approx(900.0)


def test_cross_source_without_secondary() -> None:
    assert cross_source_price_diff(prices([1]), ctx()) == []


# ---------------------------------------------------------------- identifier_conflict


def test_identifier_clean() -> None:
    assert identifier_conflict(prices([1, 2, 3]), ctx()) == []


def test_identifier_change_is_low_not_conflict() -> None:
    df = prices([1, 2, 3])
    df.loc[2, "isin"] = "INE000A01029"
    out = identifier_conflict(df, ctx())
    assert [(a.rule, a.severity) for a in out] == [("identifier_change", "low")]
    assert out[0].record_key == "AAA|2024-01-03"


def test_identifier_conflict_same_day() -> None:
    df = prices([1, 2])
    other = prices([1], symbol="AAA", isin="INE999Z01019")
    twin = prices([1], symbol="BBB")  # same ISIN, other ticker
    out = identifier_conflict(pd.concat([df, other, twin], ignore_index=True), ctx())
    rules = sorted((a.rule, a.record_key) for a in out if a.rule == "identifier_conflict")
    assert rules == [
        ("identifier_conflict", "AAA|2024-01-01"),
        ("identifier_conflict", "INE000A01011|2024-01-01"),
    ]


# ---------------------------------------------------------------- fx_rate_outlier


def fx(rates: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "rate_date": pd.bdate_range("2024-01-01", periods=len(rates)),
            "base_ccy": "USD",
            "units": 1.0,
            "rate": rates,
        }
    )


def _walk(n: int) -> list[float]:
    rng = np.random.default_rng(0)
    return list(83 * np.exp(np.cumsum(rng.normal(0, 0.002, n))))


def test_fx_outlier_clean() -> None:
    assert fx_rate_outlier(fx(_walk(60)), ctx(params={"window": 20, "z_threshold": 4})) == []


def test_fx_outlier_flags_units_error() -> None:
    rates = _walk(60)
    rates[45] = rates[44] * 1.05  # 5% jump in INR, absurd for a daily fixing
    out = fx_rate_outlier(fx(rates), ctx(params={"window": 20, "z_threshold": 4}))
    assert out and out[0].record_key.startswith("USD|") and abs(out[0].value or 0) > 4


# ---------------------------------------------------------------- macro_revision_detected


def macro(values: list[float | None]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "series_id": "GDP",
            "obs_date": pd.to_datetime(["2022-01-01", "2023-01-01"]),
            "value": values,
        }
    )


def test_macro_revision_clean() -> None:
    assert macro_revision_detected(macro([1.0, 2.0]), ctx(previous=macro([1.0, 2.0]))) == []


def test_macro_revision_flags_change_and_new_null() -> None:
    out = macro_revision_detected(macro([1.5, None]), ctx(previous=macro([1.0, 2.0])))
    assert [a.record_key for a in out] == ["GDP|2022-01-01", "GDP|2023-01-01"]
    assert "1.0 -> 1.5" in out[0].detail


def test_macro_revision_without_previous() -> None:
    assert macro_revision_detected(macro([1.0, 2.0]), ctx()) == []


def test_registry_has_all_rules() -> None:
    assert len(RULES) == 10
