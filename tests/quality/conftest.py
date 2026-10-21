"""Fixture warehouse for quality tests: tmp SQLite built with `metadata.create_all`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, create_engine, insert

from findata.model import tables as t

RUN_TS = datetime(2024, 7, 1, 12, 0, tzinfo=UTC)
DAYS = pd.bdate_range("2024-01-01", "2024-04-30")
SPIKE_DAY = DAYS[60]
SPLIT_EX = DAYS[30]


def _prices(symbol: str, base: float, seed: int, split: bool, spike: bool) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, len(DAYS))
    if spike:
        rets[DAYS.get_loc(SPIKE_DAY)] = 0.25
    close = base * np.exp(np.cumsum(rets))
    vol = rng.integers(900_000, 1_100_000, len(DAYS)).astype(float)
    if spike:
        vol[DAYS.get_loc(SPIKE_DAY)] = 9_000_000
    df = pd.DataFrame(
        {
            "trade_date": DAYS,
            "close": close,
            "open": close * (1 + rng.normal(0, 0.002, len(DAYS))),
            "volume": vol,
        }
    )
    df["high"] = df[["open", "close"]].max(axis=1) * 1.005
    df["low"] = df[["open", "close"]].min(axis=1) * 0.995
    if split:  # 1:10 split -> unadjusted price before ex-date is 10x
        pre = df["trade_date"] < SPLIT_EX
        for c in ("open", "high", "low", "close"):
            df.loc[pre, c] *= 10
        df.loc[pre, "volume"] /= 10
    df["symbol"] = symbol
    return df


def populate(eng: Engine) -> None:
    """Insert the fixture warehouse rows into an empty schema."""
    now = RUN_TS
    with eng.begin() as conn:
        conn.execute(
            insert(t.dim_exchange),
            [
                {
                    "exchange_id": 1,
                    "mic": "XNSE",
                    "name": "NSE",
                    "country": "IN",
                    "timezone": "Asia/Kolkata",
                    "currency_code": "INR",
                }
            ],
        )
        conn.execute(
            insert(t.dim_currency),
            [
                {"currency_id": 1, "code": "INR", "name": "Indian rupee"},
                {"currency_id": 2, "code": "USD", "name": "US dollar"},
                {"currency_id": 3, "code": "EUR", "name": "Euro"},
            ],
        )
        conn.execute(
            insert(t.dim_security),
            [
                {"security_id": 1, "exchange_id": 1, "symbol": "AAA", "name": "A Ltd",
                 "listing_date": datetime(2000, 1, 1).date(), "face_value": 1.0, "load_ts": now},
                {"security_id": 2, "exchange_id": 1, "symbol": "BBB", "name": "B Ltd",
                 "listing_date": None, "face_value": 10.0, "load_ts": now},
            ],
        )  # fmt: skip
        conn.execute(
            insert(t.map_security_identifier),
            [
                {"security_id": 1, "id_type": "ISIN", "id_value": "INE000A01011", "source": "nse",
                 "valid_from": DAYS[0].date(), "load_ts": now},
                {"security_id": 2, "id_type": "ISIN", "id_value": "INE000B01011", "source": "nse",
                 "valid_from": DAYS[0].date(), "load_ts": now},
                {"security_id": 1, "id_type": "FIGI", "id_value": "BBG000000001",
                 "source": "openfigi", "valid_from": DAYS[0].date(), "load_ts": now},
            ],
        )  # fmt: skip
        rows = []
        for sid, sym, base, seed, split, spike in (
            (1, "AAA", 100.0, 1, True, False),
            (2, "BBB", 50.0, 2, False, True),
        ):
            p = _prices(sym, base, seed, split, spike)
            if sym == "BBB":
                p = p.drop(index=[5, 6])  # two missing sessions
            for r in p.itertuples():
                d = r.trade_date
                common = {
                    "security_id": sid,
                    "trade_date": d.date(),
                    "open": r.open,
                    "high": r.high,
                    "low": r.low,
                    "volume": r.volume,
                    "load_ts": now,
                }
                pub = (d + pd.Timedelta(hours=13)).tz_localize("UTC").to_pydatetime()  # 18:30 IST
                isin = "INE000A01011" if sid == 1 else "INE000B01011"
                rows.append({**common, "source": "nse", "close": r.close, "source_ts": pub,
                             "isin_observed": isin})  # fmt: skip
                ycl = r.close / 10 if (split and d < SPLIT_EX) else r.close
                rows.append(
                    {**common, "source": "yahoo", "close": ycl, "source_ts": None,
                     "isin_observed": None}
                )  # fmt: skip
        conn.execute(insert(t.fact_price_daily), rows)
        conn.execute(
            insert(t.fact_corporate_action),
            [
                {"security_id": 1, "ex_date": SPLIT_EX.date(), "action_type": "split",
                 "subject": "Face Value Split From Rs 10 To Re 1", "ratio": 0.1, "amount": None,
                 "load_ts": now},
                {"security_id": 2, "ex_date": DAYS[10].date(), "action_type": "dividend",
                 "subject": "Dividend - Rs 2 Per Share", "amount": 2.0, "ratio": None,
                 "load_ts": now},
            ],
        )  # fmt: skip
        fx = []
        for d in DAYS[:20]:
            fixing = (d + pd.Timedelta(hours=8)).tz_localize("UTC").to_pydatetime()  # 13:30 IST
            fx.append({"rate_date": d.date(), "base_currency_id": 2, "quote_currency_id": 1,
                       "units": 1.0, "rate": 83.0, "source": "fbil", "source_ts": fixing,
                       "load_ts": now})  # fmt: skip
            if d != DAYS[3]:
                fx.append({"rate_date": d.date(), "base_currency_id": 3, "quote_currency_id": 1,
                           "units": 1.0, "rate": 90.0, "source": "fbil", "source_ts": fixing,
                           "load_ts": now})  # fmt: skip
        conn.execute(insert(t.fact_fx_rate), fx)
        conn.execute(
            insert(t.dim_macro_series),
            [{"series_id": 1, "source": "fred", "code": "DGS10"},
             {"series_id": 2, "source": "worldbank", "code": "NY.GDP.MKTP.CD"}],
        )  # fmt: skip
        conn.execute(
            insert(t.fact_macro_series),
            [
                {"series_id": 1, "obs_date": DAYS[0].date(), "vintage_date": DAYS[0].date(),
                 "value": 4.0, "load_ts": now},
                {"series_id": 1, "obs_date": DAYS[1].date(), "vintage_date": DAYS[0].date(),
                 "value": None, "load_ts": now},
                {"series_id": 2, "obs_date": DAYS[0].date(), "vintage_date": DAYS[0].date(),
                 "value": 3.5e12, "load_ts": now},
            ],
        )  # fmt: skip
        conn.execute(
            insert(t.dq_anomaly),
            [
                {"dataset": "nse_prices", "record_key": f"BBB|{SPIKE_DAY:%Y-%m-%d}",
                 "rule": "unexplained_jump", "severity": "high", "detail": "jump",
                 "detected_by": "rule", "symbol": "BBB", "obs_date": SPIKE_DAY.date(),
                 "status": "open", "first_seen_ts": now, "last_seen_ts": now},
                {"dataset": "nse_prices", "record_key": "AAA|2024-01-02",
                 "rule": "schema_validity", "severity": "high", "detail": "bad",
                 "detected_by": "rule", "symbol": "AAA", "obs_date": DAYS[1].date(),
                 "status": "open", "first_seen_ts": now, "last_seen_ts": now},
            ],
        )  # fmt: skip


@pytest.fixture
def wh_engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{(tmp_path / 'wh.db').as_posix()}")
    t.metadata.create_all(eng)
    populate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def no_staging() -> object:
    """A staging reader that returns OpenFIGI tickers for the fixture ISINs."""

    def read(name: str) -> pd.DataFrame:
        if name == "openfigi_mapping":
            return pd.DataFrame(
                {
                    "isin": ["INE000A01011", "INE000B01011"],
                    "ticker": ["AAA IN", "BBB IN"],
                    "name": ["A LIMITED", "ZZZ CORP"],
                }
            )
        return pd.DataFrame()

    return read
