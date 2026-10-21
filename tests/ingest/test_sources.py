"""Mocked tests for the non-NSE clients: Yahoo, FBIL, FRED, World Bank, OpenFIGI, GLEIF."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from findata.ingest import yahoo
from findata.ingest.fbil import FbilClient, parse_fbil, parse_pair
from findata.ingest.fred import FredClient, fred_user_agent, parse_fred_csv
from findata.ingest.gleif import GleifClient, best_name_match, normalise_name
from findata.ingest.openfigi import OpenFigiClient, parse_openfigi, pick_listing
from findata.ingest.worldbank import WorldBankClient, parse_worldbank
from findata.ingest.yahoo import YahooClient, parse_yahoo
from findata.settings import Settings

from .conftest import FakeResponse, FakeSession

TS = pd.Timestamp("2024-06-30", tz="UTC")


# ----------------------------------------------------------------------------- yahoo


def _yf_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="Date")
    return pd.DataFrame(
        {
            "Adj Close": [99.0, 100.0],
            "Close": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [98.0, 99.0],
            "Open": [99.0, 100.0],
            "Volume": [1000, 2000],
        },
        index=idx,
    )


def test_parse_yahoo_flat_and_multiindex() -> None:
    df = parse_yahoo(_yf_frame(), "TCS", TS)
    assert df.columns.tolist()[:3] == ["trade_date", "symbol", "open"]
    assert df["adj_close"].tolist() == [99.0, 100.0]
    multi = _yf_frame()
    multi.columns = pd.MultiIndex.from_product([multi.columns, ["TCS.NS"]])
    multi.index = multi.index.tz_localize("Asia/Kolkata")
    df2 = parse_yahoo(multi, "TCS", TS)
    assert df2["close"].tolist() == [100.0, 101.0]
    assert df2["trade_date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert parse_yahoo(pd.DataFrame(), "X", TS).empty


def test_parse_yahoo_missing_column() -> None:
    raw = _yf_frame().drop(columns=["Adj Close"])
    assert parse_yahoo(raw, "TCS", TS)["adj_close"].isna().all()


def test_yahoo_client_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(yahoo.time, "sleep", slept.append)
    seen: list[str] = []

    def downloader(t: str, s: date, e: date) -> pd.DataFrame:
        seen.append(t)
        return _yf_frame()

    df = YahooClient(downloader=downloader, sleep_s=0.5).fetch(
        date(2024, 1, 3), date(2024, 1, 31), ["TCS", "INFY"]
    )
    assert seen == ["TCS.NS", "INFY.NS"]
    assert slept == [0.5]
    assert len(df) == 2  # 2024-01-02 is outside the window
    with pytest.raises(ValueError):
        YahooClient(downloader=downloader).fetch(date(2024, 1, 1), date(2024, 1, 2), None)


# ----------------------------------------------------------------------------- fbil

FBIL = [
    {
        "processRunDate": "2024-01-10 00:00:00",
        "subProdName": "INR / 1 USD",
        "displayTime": "2024-01-10 13:30:00",
        "rate": 83.1548,
    },
    {
        "processRunDate": "2024-01-10 00:00:00",
        "subProdName": "INR / 100 JPY",
        "displayTime": None,
        "rate": 57.5,
    },
    {"processRunDate": "2024-01-10 00:00:00", "subProdName": "MIBOR", "rate": 6.8},
]


def test_parse_pair_and_fbil() -> None:
    assert parse_pair("INR / 100 JPY") == ("JPY", 100.0)
    assert parse_pair("MIBOR") is None
    df = parse_fbil(FBIL, TS)
    assert df["base_ccy"].tolist() == ["USD", "JPY"]
    assert df.loc[0, "source_ts"] == pd.Timestamp("2024-01-10 08:00", tz="UTC")
    assert pd.isna(df.loc[1, "source_ts"])
    assert df.loc[1, "units"] == 100.0


def test_fbil_client_chunks_requests(tmp_settings: Settings) -> None:
    sess = FakeSession(lambda m, u, k: FakeResponse(200, json_data=FBIL))
    df = FbilClient(session=sess, settings=tmp_settings).fetch(  # type: ignore[arg-type]
        date(2024, 1, 1), date(2024, 3, 1)
    )
    assert len(sess.calls) == 3  # 61 days / 30-day chunks
    assert sess.calls[1][2]["params"]["fromDate"] == "2024-01-31"
    assert len(df) == 2  # duplicates across chunks removed


# ----------------------------------------------------------------------------- fred

FRED_CSV = b"observation_date,DGS10\n2024-01-01,.\n2024-01-02,3.95\n"


def test_parse_fred_csv() -> None:
    df = parse_fred_csv(FRED_CSV, "DGS10", date(2026, 10, 21), TS)
    assert pd.isna(df.loc[0, "value"])
    assert df.loc[1, "value"] == pytest.approx(3.95)
    assert (df["vintage_date"] == pd.Timestamp("2026-10-21")).all()


def test_fred_client(tmp_settings: Settings) -> None:
    sess = FakeSession(lambda m, u, k: FakeResponse(200, FRED_CSV))
    client = FredClient(session=sess, settings=tmp_settings)  # type: ignore[arg-type]
    df = client.fetch(date(2024, 1, 1), date(2024, 1, 2), ["DGS10"])
    assert len(df) == 2
    assert sess.calls[0][2]["params"] == {"id": "DGS10", "cosd": "2024-01-01", "coed": "2024-01-02"}
    df_all = FredClient(session=sess, settings=tmp_settings).fetch(  # type: ignore[arg-type]
        date(2024, 1, 1), date(2024, 1, 2)
    )
    assert df_all["series_id"].nunique() == 5  # YAML series list


# ----------------------------------------------------------------------------- world bank

WB = [
    {"page": 1},
    [
        {"date": "2023", "value": 3.5e12},
        {"date": "2024", "value": None},
        {"date": "bad", "value": 1},
    ],
]


def test_parse_worldbank() -> None:
    df = parse_worldbank(WB, "NY.GDP.MKTP.CD", date(2026, 10, 21), TS)
    assert df["obs_date"].tolist() == [pd.Timestamp("2023-01-01"), pd.Timestamp("2024-01-01")]
    assert pd.isna(df.loc[1, "value"])
    assert parse_worldbank([{"message": "err"}], "X", date.today(), TS).empty


def test_worldbank_client(tmp_settings: Settings) -> None:
    sess = FakeSession(lambda m, u, k: FakeResponse(200, json_data=WB))
    client = WorldBankClient(session=sess, settings=tmp_settings, lookback_years=5)  # type: ignore[arg-type]
    df = client.fetch(date(2024, 1, 1), date(2024, 6, 30))
    assert len(sess.calls) == 3
    assert sess.calls[0][2]["params"]["date"] == "2019:2024"
    assert "IND" in sess.calls[0][1]
    assert len(df) == 6


# ----------------------------------------------------------------------------- openfigi

FIGI_JOB = {
    "data": [
        {"exchCode": "IB", "figi": "BBG000BKVPT1", "ticker": "RELIANCE"},
        {
            "exchCode": "IN",
            "figi": "BBG000BKVP93",
            "compositeFIGI": "BBG000BKVP93",
            "shareClassFIGI": "BBG001S84HH9",
            "ticker": "RELIANCE",
            "name": "RELIANCE INDUSTRIES LIMITED",
            "securityType": "Common Stock",
        },
    ]
}


def test_pick_listing_preference() -> None:
    assert pick_listing(FIGI_JOB["data"])["exchCode"] == "IN"  # type: ignore[index]
    assert pick_listing([{"exchCode": "US", "figi": "x"}])["figi"] == "x"  # type: ignore[index]
    assert pick_listing([]) is None


def test_parse_openfigi_skips_errors() -> None:
    df = parse_openfigi(
        ["INE002A01018", "INE000000000"], [FIGI_JOB, {"warning": "No identifier"}], TS
    )
    assert df["figi"].tolist() == ["BBG000BKVP93"]
    assert df.loc[0, "share_class_figi"] == "BBG001S84HH9"


def test_openfigi_client_batches_and_key(tmp_path: Any) -> None:
    settings = Settings(data_dir=tmp_path, openfigi_api_key="k123", http_min_interval_s=0)

    def handler(m: str, u: str, kw: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, json_data=[FIGI_JOB] * len(kw["json"]))

    client = OpenFigiClient(settings=settings)
    assert client.session.session.headers["X-OPENFIGI-APIKEY"] == "k123"
    client.session = FakeSession(handler)  # type: ignore[assignment]
    isins = [f"INE{i:09d}" for i in range(12)]
    df = client.fetch(symbols=isins)
    assert len(client.session.calls) == 2  # type: ignore[attr-defined]
    assert len(df) == 12
    assert OpenFigiClient(settings=settings).fetch(symbols=[]).empty


# ----------------------------------------------------------------------------- gleif


def _lei_rec(lei: str, name: str) -> dict[str, Any]:
    return {
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": name},
                "legalAddress": {"country": "IN"},
                "status": "ACTIVE",
            },
            "registration": {"status": "ISSUED"},
        },
    }


def test_normalise_and_match() -> None:
    assert normalise_name("Larsen & Toubro Ltd.") == "LARSEN AND TOUBRO LIMITED"
    recs = [
        _lei_rec("335800FL1E55CQSY7C42", "RELIANCE CHEMOTEX INDUSTRIES LIMITED"),
        _lei_rec("5493003UOETFYRONLG31", "RELIANCE INDUSTRIES LIMITED"),
    ]
    hit = best_name_match(recs, "Reliance Industries Limited")
    assert hit is not None and hit["lei"] == "5493003UOETFYRONLG31"
    assert best_name_match(recs, "Reliance Power Limited") is None


def test_gleif_client_isin_hit_name_fallback_and_parent(tmp_settings: Settings) -> None:
    def handler(m: str, url: str, kw: dict[str, Any]) -> FakeResponse:
        params = kw.get("params") or {}
        if "direct-parent" in url:
            if "5493003UOETFYRONLG31" in url:
                return FakeResponse(404)
            return FakeResponse(200, json_data={"data": {"id": "PARENTLEI00000000099"}})
        if params.get("filter[isin]") == "INE001A01010":
            return FakeResponse(200, json_data={"data": [_lei_rec("AAAAAAAAAAAAAAAAAA11", "X")]})
        if "filter[isin]" in params:
            return FakeResponse(200, json_data={"data": []})
        if params.get("filter[entity.legalName]") == "Reliance Industries Limited":
            return FakeResponse(
                200,
                json_data={
                    "data": [_lei_rec("5493003UOETFYRONLG31", "RELIANCE INDUSTRIES LIMITED")]
                },
            )
        return FakeResponse(500)

    sess = FakeSession(handler)
    client = GleifClient(session=sess, settings=tmp_settings)  # type: ignore[arg-type]
    df = client.fetch(
        symbols=["INE001A01010", "INE002A01018", "INE002A01999", "INE777A01010"],
        names={
            "INE002A01018": "Reliance Industries Limited",
            "INE002A01999": "Reliance Industries Ltd",
            "INE777A01010": "Nobody Ltd",
        },
    )
    by = df.set_index("isin")
    assert by.loc["INE001A01010", "parent_lei"] == "PARENTLEI00000000099"
    assert by.loc["INE002A01018", "lei"] == "5493003UOETFYRONLG31"
    assert pd.isna(by.loc["INE002A01018", "parent_lei"])
    assert by.loc["INE002A01999", "lei"] == "5493003UOETFYRONLG31"  # cached via name
    assert "INE777A01010" not in by.index
    assert client.lookup("INE000000000", None) is None


def test_fred_user_agent_keeps_project_identity(tmp_settings: Settings) -> None:
    ua = fred_user_agent("FinDataQualityPipeline/0.1")
    assert ua.startswith("python-requests/") and ua.endswith("FinDataQualityPipeline/0.1")
    assert (
        FredClient(settings=tmp_settings)
        .session.session.headers["User-Agent"]
        .startswith("python-requests/")
    )
