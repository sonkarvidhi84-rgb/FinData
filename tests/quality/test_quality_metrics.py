from __future__ import annotations

import math
from collections.abc import Callable

import pandas as pd
import pytest
from sqlalchemy import Engine

from findata.quality import metrics as m

from .conftest import DAYS, RUN_TS


def test_status_for_ratios_and_hours() -> None:
    assert m.status_for(0.99, 0.98) == "pass"
    assert m.status_for(0.965, 0.98) == "warn"
    assert m.status_for(0.90, 0.98) == "fail"
    assert m.status_for(None, 0.98) == "n/a"
    assert m.status_for(0.5, None) == "n/a"
    assert m.status_for(float("nan"), 0.5) == "n/a"
    assert m.status_for(5.0, 6.0, higher_is_better=False) == "pass"
    assert m.status_for(6.5, 6.0, higher_is_better=False) == "warn"
    assert m.status_for(9.0, 6.0, higher_is_better=False) == "fail"


def test_price_completeness_hand_computed() -> None:
    cal = list(pd.bdate_range("2024-01-01", "2024-01-05"))  # 5 sessions
    prices = pd.DataFrame(
        {
            "symbol": ["A"] * 5 + ["B"] * 2,
            # A has all 5; B starts on Jan 3 (expected 3) but misses Jan 4 -> 2 of 3
            "trade_date": [*cal, cal[2], cal[4]],
        }
    )
    assert m.price_completeness(prices, cal) == pytest.approx(7 / 8)


def test_price_completeness_falls_back_to_observed_dates() -> None:
    prices = pd.DataFrame(
        {"symbol": ["A", "A", "B"], "trade_date": ["2024-01-01", "2024-01-02", "2024-01-02"]}
    )
    assert m.price_completeness(prices, []) == pytest.approx(1.0)
    assert math.isnan(m.price_completeness(prices.iloc[0:0], []))


def test_nonnull_and_panel_completeness() -> None:
    df = pd.DataFrame({"a": [1, None], "b": [1, 2]})
    assert m.nonnull_ratio(df, ["a", "b"]) == pytest.approx(0.75)
    assert math.isnan(m.nonnull_ratio(df, ["zzz"]))
    fx = pd.DataFrame({"d": [1, 1, 2], "c": ["USD", "EUR", "USD"]})
    assert m.panel_completeness(fx, "d", "c") == pytest.approx(3 / 4)
    assert math.isnan(m.panel_completeness(fx.iloc[0:0], "d", "c"))


def test_timeliness_hours_relative_to_ist_close() -> None:
    dates = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    # 15:30 IST = 10:00 UTC; published 12:00 and 14:00 UTC -> 2h and 4h
    ts = pd.Series(pd.to_datetime(["2024-01-02 12:00", "2024-01-03 14:00"], utc=True))
    med, p95 = m.timeliness_hours(dates, ts)
    assert med == pytest.approx(3.0)
    assert p95 == pytest.approx(3.9)
    nan_med, _ = m.timeliness_hours(dates, pd.Series([pd.NaT, pd.NaT]))
    assert math.isnan(nan_med)


def test_split_factors() -> None:
    nse = pd.DataFrame(
        {
            "symbol": ["X", "X", "X"],
            "trade_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            "close": [1000.0, 101.0, 102.0],
        }
    )
    acts = pd.DataFrame(
        {
            "symbol": ["X"],
            "ex_date": pd.to_datetime(["2024-01-02"]),
            "action_type": ["split"],
            "ratio": [0.1],
        }
    )
    assert list(m.split_factors(nse, acts)) == [0.1, 1.0, 1.0]
    assert list(m.split_factors(nse, None)) == [1.0, 1.0, 1.0]


def test_accuracy_on_returns_is_immune_to_vendor_restatement() -> None:
    d = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    nse = pd.DataFrame(
        {"symbol": ["B"] * 4 + ["C"] * 4, "trade_date": [*d, *d],
         "close": [200.0, 202.0, 204.0, 206.0, 50.0, 51.0, 52.0, 53.0]}
    )  # fmt: skip
    # B: Yahoo restated for a later 1:1 bonus (constant 0.5x); C: one bad Yahoo print
    yahoo = nse.assign(close=[100.0, 101.0, 102.0, 103.0, 50.0, 51.0, 60.0, 53.0])
    # 6 comparable returns; C's bad day breaks two of them (into and out of the print)
    assert m.accuracy_share(nse, yahoo, 0.5) == pytest.approx(4 / 6)
    assert m.basis_mismatch_symbols(nse, yahoo, 0.5) == ["B"]
    assert math.isnan(m.accuracy_share(nse, yahoo.iloc[0:0], 0.5))
    assert m.basis_mismatch_symbols(nse, yahoo.iloc[0:0], 0.5) == []


def test_ohlc_sanity_share() -> None:
    df = pd.DataFrame(
        {"open": [10, 10, 10], "high": [11, 9, 11], "low": [9, 9, -1], "close": [10.5, 10, 10]}
    )
    assert m.ohlc_sanity_share(df) == pytest.approx(1 / 3)
    assert math.isnan(m.ohlc_sanity_share(df.iloc[0:0]))


def test_isin_name_agreement_ignores_legal_form() -> None:
    figi = pd.DataFrame(
        {
            "isin": ["I1", "I2", "I3"],
            "name": ["RELIANCE INDUSTRIES LTD", "INFOSYS LTD", "SOMETHING ELSE"],
        }
    )
    nse = pd.DataFrame(
        {
            "isin": ["I1", "I2", "I3"],
            "name": ["Reliance Industries Limited", "Infosys Limited", "Tata Steel Limited"],
        }
    )
    assert m.isin_name_agreement(figi, nse) == pytest.approx(2 / 3)
    assert math.isnan(m.isin_name_agreement(figi.iloc[0:0], nse))
    assert math.isnan(m.isin_name_agreement(figi.assign(name=None), nse))


def test_violation_share_complement() -> None:
    assert m.violation_share_complement(100, 1) == pytest.approx(0.99)
    assert math.isnan(m.violation_share_complement(0, 0))


def test_compute_all_on_fixture_warehouse(
    wh_engine: Engine, no_staging: Callable[[str], pd.DataFrame]
) -> None:
    df = m.compute_all(
        wh_engine, "r1", RUN_TS, expected_days=lambda: list(DAYS), staging=no_staging
    )
    assert list(df.columns) == m.DQ_METRIC_COLUMNS
    assert not df.duplicated(["dataset", "metric"]).any()
    v = df.set_index(["dataset", "metric"])["value"]
    n = len(DAYS)
    assert v["nse_prices", "row_completeness"] == pytest.approx((2 * n - 2) / (2 * n))
    assert v["nse_prices", "publish_lag_median_hours"] == pytest.approx(3.0)
    assert v["nse_prices", "daily_return_agreement_vs_yahoo"] == pytest.approx(1.0)
    assert v["nse_prices", "ohlc_sanity_share"] == pytest.approx(1.0)
    assert v["nse_prices", "validity_share"] == pytest.approx(1 - 1 / (2 * n - 2))
    assert v["fx_rates", "pair_date_completeness"] == pytest.approx(39 / 40)
    assert v["fx_rates", "publish_lag_median_hours"] == pytest.approx(0.0)
    assert v["fred_macro", "value_completeness"] == pytest.approx(0.5)
    assert v["openfigi_mapping", "securities_with_figi"] == pytest.approx(0.5)
    assert v["openfigi_mapping", "isin_name_agreement"] == pytest.approx(0.5)
    assert v["nse_prices", "symbols_with_price_basis_mismatch"] == 0
    assert v["nse_securities", "attribute_completeness"] == pytest.approx(7 / 8)
    st = df.set_index(["dataset", "metric"])["status"]
    assert st["nse_prices", "row_completeness"] == "warn"  # 0.9884 vs 0.99 min
    assert st["fred_macro", "value_completeness"] == "fail"
    assert st["nse_prices", "publish_lag_p95_hours"] == "n/a"


def test_latest_vintage() -> None:
    macro = pd.DataFrame(
        {"source": ["fred"] * 2, "series_code": ["X"] * 2, "obs_date": ["2024-01-01"] * 2,
         "vintage_date": ["2024-02-01", "2024-03-01"], "value": [1.0, 1.1]}
    )  # fmt: skip
    assert m.latest_vintage(macro)["value"].tolist() == [1.1]
    assert m.latest_vintage(macro.iloc[0:0]).empty
