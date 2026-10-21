from collections.abc import Iterator

import pytest
from model.conftest import SYMBOLS, build_frames, staged_settings  # noqa: F401
from sqlalchemy import Engine

from findata.analytics import queries as q
from findata.model.db import get_engine, upgrade
from findata.model.loaders import load_frames
from findata.settings import Settings


@pytest.fixture
def engine(staged_settings: Settings) -> Iterator[Engine]:  # noqa: F811
    upgrade(staged_settings.db_url)
    eng = get_engine(staged_settings.db_url)
    load_frames(eng, build_frames(), None, symbols=SYMBOLS)
    yield eng
    eng.dispose()


@pytest.fixture
def empty_engine(staged_settings: Settings) -> Iterator[Engine]:  # noqa: F811
    upgrade(staged_settings.db_url)
    eng = get_engine(staged_settings.db_url)
    yield eng
    eng.dispose()


def test_adjusted_prices_remove_split_gap(engine: Engine) -> None:
    px = q.adjusted_prices(engine)
    nestle = px[px["symbol"] == "NESTLEIND"].set_index("trade_date")["adjusted_close"]
    daily = nestle.pct_change().dropna()
    assert daily.abs().max() < 0.1  # the raw 90% drop is gone


def test_dividend_event_returns(engine: Engine) -> None:
    out = q.dividend_event_returns(engine, window=5)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["symbol"] == "TCS" and row["sector"] == "Information Technology"
    assert row["n_peers"] == 1  # INFY is the only IT peer loaded
    assert row["excess_return"] == pytest.approx(row["stock_return"] - row["sector_return"])
    assert abs(row["stock_return"]) < 0.2


def test_dividend_window_beyond_data_is_skipped(engine: Engine) -> None:
    assert q.dividend_event_returns(engine, window=100).empty


def test_inr_hedged_vs_unhedged(engine: Engine) -> None:
    out = q.inr_hedged_vs_unhedged(engine)
    assert set(out["symbol"]) == set(SYMBOLS)
    # rupee weakens through the fixture (83.00 -> 83.44) so unhedged < hedged
    assert (out["usd_unhedged_return"] < out["usd_hedged_return"]).all()
    r = out.iloc[0]
    expected = (1 + r["inr_return"]) * (r["fx_start"] / r["fx_end"]) - 1
    assert r["usd_unhedged_return"] == pytest.approx(expected)


def test_macro_latest_uses_latest_vintage(engine: Engine) -> None:
    y = q.macro_latest(engine, "DGS10")
    assert y.iloc[0] == pytest.approx(4.0)  # 2024-02-01 vintage beats 2024-01-15 (3.5)


@pytest.mark.parametrize("lag", [0, 1])
def test_us10y_correlation(engine: Engine, lag: int) -> None:
    out = q.nifty_vs_us10y_correlation(engine, lag_days=lag)
    assert "UNIVERSE" in set(out["symbol"])
    assert out["correlation"].between(-1, 1).all()
    assert (out["n_obs"] >= 3).all()


def test_empty_warehouse_returns_empty_frames(empty_engine: Engine) -> None:
    assert q.dividend_event_returns(empty_engine).empty
    assert q.inr_hedged_vs_unhedged(empty_engine).empty
    assert q.nifty_vs_us10y_correlation(empty_engine).empty


def test_asof() -> None:
    import pandas as pd

    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2024-01-02", "2024-01-04"]))
    assert q._asof(s, pd.Timestamp("2024-01-03")) == 1.0
    assert q._asof(s, pd.Timestamp("2024-01-01")) == 1.0
    assert q._asof(s.iloc[:0], pd.Timestamp("2024-01-01")) is None
