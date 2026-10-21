from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from findata.config import load_dataset
from findata.settings import Settings
from findata.store import coerce
from findata.validate import engine
from findata.validate.engine import run_validation, split_vintages, validate_frame
from findata.validate.schemas import build_schema, validate_schema

TS = "2024-01-05T12:00:00Z"


def nse_rows() -> pd.DataFrame:
    days = pd.bdate_range("2024-01-01", periods=6)
    closes = [100.0, 101.0, 102.0, 103.0, 10.0, 10.2]  # crash on day 5, no action
    return pd.DataFrame(
        {
            "trade_date": days.strftime("%Y-%m-%d"),
            "symbol": "AAA",
            "series": "EQ",
            "isin": "INE000A01011",
            "name": "AAA LTD",
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "prev_close": [None, *closes[:-1]],
            "volume": 1000.0,
            "turnover": 1.0,
            "trades": 10.0,
            "source_ts": TS,
            "ingest_ts": TS,
        }
    )


@pytest.fixture()
def sample_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    samples = tmp_path / "samples"
    samples.mkdir()
    nse = nse_rows()
    bad = nse.iloc[[2]].copy()  # duplicate key + invalid ISIN-free row
    bad2 = nse.iloc[[3]].copy()
    bad2["isin"] = "BADISIN"
    bad2["trade_date"] = "2024-01-10"
    pd.concat([nse.drop(index=1), bad, bad2]).to_csv(samples / "nse_prices.csv", index=False)
    yahoo = nse[["trade_date", "symbol", "open", "high", "low", "close"]].assign(
        adj_close=nse["close"], volume=1000.0, ingest_ts=TS
    )
    yahoo.to_csv(samples / "yahoo_prices.csv", index=False)
    pd.DataFrame(
        {
            "symbol": ["AAA"],
            "series": ["EQ"],
            "isin": ["INE000A01011"],
            "company": ["AAA"],
            "subject": ["Dividend - Rs 1 Per Share"],
            "ex_date": ["2024-01-02"],
            "record_date": ["2024-01-02"],
            "face_value": [10.0],
            "action_type": ["dividend"],
            "ratio": [None],
            "amount": [1.0],
            "ingest_ts": [TS],
        }
    ).to_csv(samples / "nse_corporate_actions.csv", index=False)
    pd.DataFrame(
        {
            "series_id": ["DGS10", "DGS10", "DGS10"],
            "obs_date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "value": [4.0, 4.1, 4.2],
            "vintage_date": ["2024-02-01", "2024-03-01", "2024-03-01"],
            "ingest_ts": [TS, TS, TS],
        }
    ).to_csv(samples / "fred_macro.csv", index=False)
    monkeypatch.setattr(
        engine, "trading_days", lambda: list(pd.bdate_range("2024-01-01", "2024-01-10"))
    )
    return Settings(data_dir=tmp_path, input_mode="samples")


def test_run_validation_all_on_samples(sample_env: Settings) -> None:
    summary = run_validation("all", "samples", settings=sample_env)
    assert (
        summary["nse_prices"]["rows"] == 5
    )  # 6 days - 1 dropped; duplicate and bad-ISIN rows removed
    anoms = pd.read_parquet(sample_env.staging_dir / "anomalies.parquet")
    rules = set(anoms["rule"])
    assert {
        "schema_validity",
        "duplicate_keys",
        "missing_trading_days",
        "unexplained_jump",
        "macro_revision_detected",
    } <= rules
    assert set(anoms["severity"]) <= {"low", "medium", "high"}
    fred = pd.read_parquet(sample_env.staging_dir / "fred_macro.parquet")
    assert len(fred) == 3  # all vintages kept in staging
    assert summary["gleif_lei"] == {"rows": 0, "anomalies": 0}


def test_run_validation_single_dataset_replaces_only_its_anomalies(sample_env: Settings) -> None:
    run_validation("all", "samples", settings=sample_env)
    before = pd.read_parquet(sample_env.staging_dir / "anomalies.parquet")
    run_validation("fred_macro", "samples", settings=sample_env)
    after = pd.read_parquet(sample_env.staging_dir / "anomalies.parquet")
    assert len(before) == len(after)
    assert (after["dataset"] == "fred_macro").sum() == (before["dataset"] == "fred_macro").sum()


def test_unknown_dataset_raises() -> None:
    with pytest.raises(KeyError):
        run_validation("nope")


def test_schema_clean_and_dirty() -> None:
    cfg = load_dataset("nse_prices")
    clean = coerce(nse_rows(), cfg)
    valid, anoms = validate_schema(clean, cfg)
    assert anoms == [] and len(valid) == len(clean)
    dirty = clean.copy()
    dirty.loc[0, "isin"] = "XX"
    dirty.loc[1, "close"] = -5.0
    valid, anoms = validate_schema(dirty, cfg)
    assert len(valid) == len(clean) - 2
    assert {a.record_key for a in anoms} == {"XX|2024-01-01", "INE000A01011|2024-01-02"}
    assert all(a.rule == "schema_validity" for a in anoms)


def test_schema_missing_column_is_not_row_level() -> None:
    cfg = load_dataset("fx_rates")
    df = pd.DataFrame({"rate_date": pd.to_datetime(["2024-01-01"]), "base_ccy": ["USD"]})
    valid, anoms = validate_schema(df, cfg)
    assert len(valid) == 1
    assert anoms and anoms[0].record_key.startswith("column:")


def test_build_schema_uses_vintage_in_key() -> None:
    schema = build_schema(load_dataset("fred_macro"))
    assert "vintage_date" in (schema.unique or [])


def test_validate_frame_empty_and_split_vintages() -> None:
    cfg = load_dataset("fred_macro")
    empty = coerce(pd.DataFrame(), cfg)
    assert validate_frame(empty, cfg) == (empty, [])
    latest, prev = split_vintages(empty, cfg)
    assert latest.empty and prev.empty
