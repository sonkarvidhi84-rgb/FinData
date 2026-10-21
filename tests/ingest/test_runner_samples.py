from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from findata.calendar import trading_days
from findata.ingest import runner
from findata.ingest.base import write_partitions
from findata.ingest.runner import build_calendar_yaml, run_ingest, universe_isins
from findata.ingest.samples import build_samples, truncate
from findata.settings import Settings

TS = pd.Timestamp("2024-06-30", tz="UTC")


def test_build_calendar_yaml_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nse.yaml"
    avail = [date(2024, 1, d) for d in (1, 2, 3, 5, 6)]  # Jan 4 Thu missing, Sat Jan 6 special
    spec = build_calendar_yaml(
        date(2024, 1, 1), date(2024, 1, 7), avail, {date(2024, 1, 4): "Test Day"}, path
    )
    assert spec["holidays"] == [{"date": "2024-01-04", "name": "Test Day"}]
    assert spec["special_sessions"][0]["date"] == "2024-01-06"
    assert yaml.safe_load(path.read_text())["start"] == "2024-01-01"
    days = trading_days(path=path)
    assert [d.day for d in days] == [1, 2, 3, 5, 6]


def _seed_raw(s: Settings) -> None:
    prices = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-05", "2023-12-29"]),
            "symbol": ["NESTLEIND", "NESTLEIND", "OTHERCO", "NESTLEIND"],
            "series": "EQ",
            "isin": ["INE239A01016", "INE239A01024", "INE000X01010", "INE239A01016"],
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10.0,
            "ingest_ts": TS,
        }
    )
    write_partitions(prices, "nse", "nse_prices", "trade_date", s)
    master = pd.DataFrame(
        {
            "symbol": ["NESTLEIND", "OTHERCO"],
            "company": ["Nestle India Limited", "Other Co"],
            "isin": ["INE239A01024", "INE000X01010"],
            "snapshot_date": pd.Timestamp("2026-10-21"),
        }
    )
    write_partitions(master, "nse", "nse_securities", "snapshot_date", s)
    fx = pd.DataFrame(
        {
            "rate_date": pd.to_datetime(["2024-01-05", "2025-01-01"]),
            "base_ccy": "USD",
            "quote_ccy": "INR",
            "units": 1.0,
            "rate": 83.0,
            "ingest_ts": TS,
        }
    )
    write_partitions(fx, "fbil", "fx_rates", "rate_date", s)


def test_universe_isins_includes_old_isin(tmp_settings: Settings) -> None:
    _seed_raw(tmp_settings)
    isins, names = universe_isins(tmp_settings)
    assert isins == ["INE239A01016", "INE239A01024"]
    assert names["INE239A01016"] == "Nestle India Limited"


def test_build_samples_filters_universe_and_window(tmp_settings: Settings) -> None:
    _seed_raw(tmp_settings)
    out = build_samples(date(2024, 1, 1), date(2024, 6, 30), tmp_settings)
    assert out["nse_prices"] == 2
    assert out["nse_securities"] == 1
    assert out["fx_rates"] == 1
    back = pd.read_csv(tmp_settings.samples_dir / "nse_prices.csv")
    assert set(back["symbol"]) == {"NESTLEIND"}
    # samples are readable through the store in canonical shape
    from findata.store import load_frame

    df = load_frame("nse_prices", mode="samples", settings=tmp_settings)
    assert df["ingest_ts"].dt.tz is not None and len(df) == 2


def test_build_samples_rejects_large_files(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_raw(tmp_settings)
    monkeypatch.setattr("findata.ingest.samples.MAX_BYTES", 10)
    with pytest.raises(ValueError, match="1 MB"):
        build_samples(date(2024, 1, 1), date(2024, 6, 30), tmp_settings)


def test_truncate_empty() -> None:
    assert truncate(pd.DataFrame(), "nse_prices", date(2024, 1, 1), date(2024, 1, 2), []).empty


class _Stub:
    def __init__(self, source: str, dataset: str, part: str, frame: pd.DataFrame) -> None:
        self.source, self.dataset, self.partition_col, self.frame = source, dataset, part, frame
        self.calls: list[Any] = []
        self.available_days: list[date] = [date(2024, 1, 5)]
        self.session = None

    def fetch(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        self.calls.append((args, kwargs))
        return self.frame


def test_run_ingest_all_with_stub_clients(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_raw(tmp_settings)
    d = pd.Timestamp("2024-01-05")
    frames = {
        "NseSecuritiesClient": _Stub(
            "nse",
            "nse_securities",
            "snapshot_date",
            pd.DataFrame(
                {
                    "symbol": ["NESTLEIND"],
                    "company": ["Nestle India Limited"],
                    "isin": ["INE239A01024"],
                    "snapshot_date": [d],
                }
            ),
        ),
        "NseBhavcopyClient": _Stub(
            "nse", "nse_prices", "trade_date", pd.DataFrame({"trade_date": [d], "symbol": ["X"]})
        ),
        "NseCorporateActionsClient": _Stub(
            "nse", "nse_corporate_actions", "ex_date", pd.DataFrame({"ex_date": [d]})
        ),
        "FbilClient": _Stub("fbil", "fx_rates", "rate_date", pd.DataFrame({"rate_date": [d]})),
        "FredClient": _Stub(
            "fred", "fred_macro", "vintage_date", pd.DataFrame({"vintage_date": [d]})
        ),
        "WorldBankClient": _Stub(
            "worldbank", "worldbank_macro", "vintage_date", pd.DataFrame({"vintage_date": [d]})
        ),
        "OpenFigiClient": _Stub(
            "openfigi", "openfigi_mapping", "snapshot", pd.DataFrame({"isin": ["A"]})
        ),
        "GleifClient": _Stub("gleif", "gleif_lei", "snapshot", pd.DataFrame({"isin": ["A"]})),
        "YahooClient": _Stub(
            "yahoo", "yahoo_prices", "trade_date", pd.DataFrame({"trade_date": [d]})
        ),
    }
    for cls, stub in frames.items():
        monkeypatch.setattr(runner, cls, lambda *a, _s=stub, **k: _s)

    def boom(session: Any) -> dict[date, str]:
        raise RuntimeError("no names")

    monkeypatch.setattr(runner, "fetch_holiday_names", boom)
    cal = tmp_path / "cal.yaml"
    monkeypatch.setattr(runner, "NSE_CALENDAR_PATH", cal)
    monkeypatch.setattr(
        runner,
        "build_calendar_yaml",
        lambda s, e, a, n: build_calendar_yaml(s, e, a, n, path=cal),
    )
    out = run_ingest("all", date(2024, 1, 5), date(2024, 1, 5), ["NESTLEIND"], tmp_settings)
    assert set(out) == {
        "nse_securities",
        "nse_prices",
        "nse_corporate_actions",
        "yahoo_prices",
        "fx_rates",
        "fred_macro",
        "worldbank_macro",
        "openfigi_mapping",
        "gleif_lei",
    }
    assert cal.exists()
    gleif_kwargs = frames["GleifClient"].calls[0][1]
    assert "INE239A01016" in gleif_kwargs["symbols"]
    assert gleif_kwargs["names"]["INE239A01016"] == "Nestle India Limited"


def test_run_ingest_rejects_unknown_source(tmp_settings: Settings) -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        run_ingest("bloomberg", date(2024, 1, 1), date(2024, 1, 2), None, tmp_settings)
