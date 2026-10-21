"""Tiny synthetic staging datasets (canonical shape) for loader and analytics tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from findata.config import load_dataset
from findata.settings import Settings
from findata.store import coerce, write_staging

SYMBOLS = ["RELIANCE", "TCS", "INFY", "NESTLEIND"]
ISINS = {
    "RELIANCE": "INE002A01018",
    "TCS": "INE467B01029",
    "INFY": "INE009A01021",
}
NESTLE_OLD, NESTLE_NEW = "INE239A01016", "INE239A01024"
DAYS = pd.bdate_range("2024-01-01", "2024-01-31")
INGEST = pd.Timestamp("2024-02-01 00:00", tz="UTC")


def _prices() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    base = {"RELIANCE": 2500.0, "TCS": 3700.0, "INFY": 1500.0, "NESTLEIND": 25000.0}
    for sym in SYMBOLS:
        px = base[sym]
        for d in DAYS:
            px *= 1 + rng.normal(0, 0.01)
            close = px
            isin = ISINS.get(sym, NESTLE_OLD)
            if sym == "NESTLEIND" and d >= pd.Timestamp("2024-01-05"):
                close = px / 10  # 1:10 split, ex 2024-01-05
                isin = NESTLE_NEW
            if sym == "TCS" and d >= pd.Timestamp("2024-01-10"):
                close = px - 10  # Rs 10 dividend, ex 2024-01-10
            rows.append(
                {
                    "trade_date": d,
                    "symbol": sym,
                    "series": "EQ",
                    "isin": isin,
                    "name": f"{sym} LTD",
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "prev_close": None,
                    "volume": 1000.0,
                    "turnover": 1000.0 * close,
                    "trades": 10.0,
                    "source_ts": d + pd.Timedelta(hours=12),
                    "ingest_ts": INGEST,
                }
            )
    return pd.DataFrame(rows)


def build_frames() -> dict[str, pd.DataFrame]:
    prices = _prices()
    yahoo = prices.copy()
    nestle_pre = (yahoo["symbol"] == "NESTLEIND") & (yahoo["trade_date"] < "2024-01-05")
    yahoo.loc[nestle_pre, "close"] = yahoo.loc[nestle_pre, "close"] / 10
    yahoo["adj_close"] = yahoo["close"]
    ca = pd.DataFrame(
        [
            {
                "symbol": "NESTLEIND",
                "series": "EQ",
                "isin": NESTLE_OLD,
                "company": "Nestle India Limited",
                "subject": (
                    "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"
                ),
                "ex_date": "2024-01-05",
                "record_date": "2024-01-05",
                "face_value": 10.0,
                "action_type": "split",
                "ratio": 0.1,
                "amount": None,
                "ingest_ts": INGEST,
            },
            {
                "symbol": "TCS",
                "series": "EQ",
                "isin": ISINS["TCS"],
                "company": "Tata Consultancy Services Limited",
                "subject": "Interim Dividend - Rs 10 Per Share",
                "ex_date": "2024-01-10",
                "record_date": "2024-01-10",
                "face_value": 1.0,
                "action_type": "dividend",
                "ratio": None,
                "amount": 10.0,
                "ingest_ts": INGEST,
            },
            {
                "symbol": "ZZZOTHER",
                "series": "EQ",
                "isin": "INE000000000",
                "company": "Not in universe",
                "subject": "Bonus 1:1",
                "ex_date": "2024-01-11",
                "record_date": None,
                "face_value": 10.0,
                "action_type": "bonus",
                "ratio": 0.5,
                "amount": None,
                "ingest_ts": INGEST,
            },
        ]
    )
    master = pd.DataFrame(
        [
            {
                "symbol": s,
                "company": f"{s} Limited",
                "series": "EQ",
                "listing_date": "1995-11-29",
                "isin": ISINS.get(s, NESTLE_NEW),
                "face_value": 1.0,
                "snapshot_date": "2024-02-01",
            }
            for s in SYMBOLS
        ]
    )
    fx = pd.DataFrame(
        [
            {
                "rate_date": d,
                "base_ccy": c,
                "quote_ccy": "INR",
                "units": 100.0 if c == "JPY" else 1.0,
                "rate": (83.0 + i * 0.02) if c == "USD" else 90.0,
                "source_ts": d + pd.Timedelta(hours=8),
                "ingest_ts": INGEST,
            }
            for i, d in enumerate(DAYS)
            for c in ("USD", "EUR")
        ]
    )
    us_days = pd.bdate_range("2023-12-28", "2024-01-31")
    fred = pd.DataFrame(
        [
            {
                "series_id": "DGS10",
                "obs_date": d,
                "value": 4.0 + 0.01 * i,
                "vintage_date": "2024-02-01",
                "ingest_ts": INGEST,
            }
            for i, d in enumerate(us_days)
        ]
        + [
            {
                "series_id": "DGS10",
                "obs_date": us_days[0],
                "value": 3.5,
                "vintage_date": "2024-01-15",
                "ingest_ts": INGEST,
            }
        ]
    )
    wb = pd.DataFrame(
        [
            {
                "series_id": "NY.GDP.MKTP.CD",
                "obs_date": "2022-01-01",
                "value": 3.35e12,
                "vintage_date": "2024-02-01",
                "ingest_ts": INGEST,
            },
            {
                "series_id": "XX.NEW.SERIES",
                "obs_date": "2022-01-01",
                "value": 1.0,
                "vintage_date": "2024-02-01",
                "ingest_ts": INGEST,
            },
        ]
    )
    figi = pd.DataFrame(
        [
            {
                "isin": isin,
                "figi": f"BBG00000000{i}",
                "composite_figi": f"BBG00000000{i}",
                "share_class_figi": None,
                "ticker": s,
                "name": s,
                "security_type": "Common Stock",
                "exch_code": "IN",
                "ingest_ts": INGEST,
            }
            for i, (s, isin) in enumerate(ISINS.items())
        ]
    )
    gleif = pd.DataFrame(
        [
            {
                "isin": ISINS["RELIANCE"],
                "lei": "5493003D0B2A9HQQ7J46",
                "legal_name": "RELIANCE INDUSTRIES LIMITED",
                "country": "IN",
                "entity_status": "ACTIVE",
                "registration_status": "ISSUED",
                "parent_lei": None,
                "ingest_ts": INGEST,
            }
        ]
    )
    raw = {
        "nse_prices": prices,
        "yahoo_prices": yahoo,
        "nse_corporate_actions": ca,
        "nse_securities": master,
        "fx_rates": fx,
        "fred_macro": fred,
        "worldbank_macro": wb,
        "openfigi_mapping": figi,
        "gleif_lei": gleif,
    }
    return {name: coerce(df, load_dataset(name)) for name, df in raw.items()}


def anomalies_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "dataset": "nse_prices",
                "rule": "unexplained_jump",
                "severity": "high",
                "record_key": "NESTLEIND|2024-01-05",
                "detail": "close fell 90%",
                "symbol": "NESTLEIND",
                "obs_date": "2024-01-05",
                "value": -0.9,
                "detected_by": "rule",
            },
            {
                "dataset": "fx_rates",
                "rule": "fx_rate_outlier",
                "severity": "medium",
                "record_key": "USD|2024-01-10",
                "detail": "z=4.2",
                "symbol": None,
                "obs_date": None,
                "value": None,
                "detected_by": "statistical",
            },
        ]
    )


@pytest.fixture
def frames() -> dict[str, pd.DataFrame]:
    return build_frames()


@pytest.fixture
def staged_settings(tmp_path: Path) -> Settings:
    """Settings pointing at a tmp data dir with every dataset staged."""
    s = Settings(data_dir=tmp_path, database_url="")
    for name, df in build_frames().items():
        write_staging(df, name, s)
    anomalies_frame().to_parquet(s.staging_dir / "anomalies.parquet", index=False)
    return s
