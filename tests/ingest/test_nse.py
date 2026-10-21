from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from findata.ingest import nse
from findata.ingest.nse import (
    NseBhavcopyClient,
    NseCorporateActionsClient,
    NseSecuritiesClient,
    fetch_holiday_names,
    parse_bhavcopy,
    parse_corporate_actions,
    parse_equity_master,
    parse_last_modified,
    parse_subject,
    unzip_single_csv,
)
from findata.settings import Settings

from .conftest import FakeResponse, FakeSession, bhav_csv, make_zip

TS = pd.Timestamp("2024-01-05 13:00", tz="UTC")


def test_parse_bhavcopy_keeps_eq_stocks_only() -> None:
    df = parse_bhavcopy(bhav_csv(), TS, TS)
    assert df["symbol"].tolist() == ["RELIANCE", "NESTLEIND"]
    row = df.iloc[0]
    assert row["isin"] == "INE002A01018"
    assert row["close"] == pytest.approx(2610.5)
    assert row["prev_close"] == pytest.approx(2595.0)
    assert row["volume"] == 1000
    assert row["trade_date"] == pd.Timestamp("2024-01-05")
    assert row["source_ts"] == TS


def test_unzip_and_last_modified() -> None:
    assert unzip_single_csv(make_zip("x.csv", b"a,b\n")) == b"a,b\n"
    lm = parse_last_modified("Fri, 05 Jan 2024 13:05:00 GMT")
    assert lm == pd.Timestamp("2024-01-05 13:05", tz="UTC")
    assert parse_last_modified(None) is None
    assert parse_last_modified("garbage") is None


def _bhav_handler(method: str, url: str, kw: dict[str, Any]) -> FakeResponse:
    if "20240105" in url:
        return FakeResponse(
            200,
            make_zip("b.csv", bhav_csv()),
            headers={"Last-Modified": "Fri, 05 Jan 2024 13:05:00 GMT"},
        )
    return FakeResponse(404)


def test_bhavcopy_fetch_records_missing_days_and_caches(tmp_settings: Settings) -> None:
    sess = FakeSession(_bhav_handler)
    client = NseBhavcopyClient(session=sess, settings=tmp_settings)  # type: ignore[arg-type]
    df = client.fetch(date(2024, 1, 5), date(2024, 1, 6))
    assert len(df) == 2
    assert client.available_days == [date(2024, 1, 5)]
    assert client.missing_days == [date(2024, 1, 6)]
    assert df["source_ts"].iloc[0] == pd.Timestamp("2024-01-05 13:05", tz="UTC")
    n_calls = len(sess.calls)
    again = client.fetch(date(2024, 1, 5), date(2024, 1, 6), ["RELIANCE"])
    assert len(sess.calls) == n_calls  # zip and 404 marker both served from cache
    assert again["symbol"].tolist() == ["RELIANCE"]
    assert again["source_ts"].iloc[0] == pd.Timestamp("2024-01-05 13:05", tz="UTC")


def test_bhavcopy_fetch_no_sessions(tmp_settings: Settings) -> None:
    sess = FakeSession(lambda m, u, k: FakeResponse(404))
    client = NseBhavcopyClient(session=sess, settings=tmp_settings)  # type: ignore[arg-type]
    df = client.fetch(date(2024, 1, 6), date(2024, 1, 7))
    assert df.empty
    assert "close" in df.columns


@pytest.mark.parametrize(
    ("subject", "action", "ratio", "amount"),
    [
        ("Dividend - Rs 9 Per Share", "dividend", None, 9.0),
        ("Interim Dividend - Re 0.50 Per Share", "dividend", None, 0.5),
        ("Interim Dividend - Rs  5 Per Sh", "dividend", None, 5.0),
        (
            "Dividend - Rs 10 Per Share & Special Dividend - Rs 5 Per Share",
            "dividend",
            None,
            15.0,
        ),
        ("Annual General Meeting/Dividend - Rs 21 Per Share", "dividend", None, 21.0),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
            "split",
            0.1,
            None,
        ),
        (
            "Face Value Split (Sub-Division) - From Rs10/- Per Share To Rs 2/- Per Share",
            "split",
            0.2,
            None,
        ),
        ("Face Value Split (Sub-Division)", "split", None, None),
        ("Bonus 1:1", "bonus", 0.5, None),
        ("Bonus 3:2", "bonus", 0.4, None),
        ("Rights 1:10 @ Premium Rs 100/-", "rights", None, None),
        ("Buy Back", "buyback", None, None),
        (
            "Distribution - Rs 2.5 Per Unit Consisting Of Interest Rs 2 Per Unit",
            "dividend",
            None,
            2.5,
        ),
        ("Annual General Meeting", "other", None, None),
        ("Demerger", "other", None, None),
    ],
)
def test_parse_subject(
    subject: str, action: str, ratio: float | None, amount: float | None
) -> None:
    got = parse_subject(subject)
    assert got["action_type"] == action
    assert got["ratio"] == (pytest.approx(ratio) if ratio is not None else None)
    assert got["amount"] == (pytest.approx(amount) if amount is not None else None)


CA_RECORDS = [
    {
        "symbol": "NESTLEIND",
        "series": "EQ",
        "isin": "INE239A01016",
        "comp": "Nestle India Limited",
        "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
        "exDate": "05-Jan-2024",
        "recDate": "05-Jan-2024",
        "faceVal": "10",
    },
    {
        "symbol": "TCS",
        "series": "EQ",
        "isin": "INE467B01029",
        "comp": "TCS",
        "subject": "Interim Dividend - Rs 9 Per Share",
        "exDate": "19-Jan-2024",
        "recDate": "-",
        "faceVal": "",
    },
    {"symbol": "BAD", "subject": "x", "exDate": "-"},
    {"symbol": "BAD2", "subject": "x", "exDate": "31/01/2024"},
]


def test_parse_corporate_actions() -> None:
    df = parse_corporate_actions(CA_RECORDS, TS)
    assert df["symbol"].tolist() == ["NESTLEIND", "TCS"]
    assert df.loc[0, "ratio"] == pytest.approx(0.1)
    assert df.loc[0, "face_value"] == 10.0
    assert pd.isna(df.loc[1, "record_date"])
    assert pd.isna(df.loc[1, "face_value"])
    assert df.loc[1, "amount"] == 9.0


def test_corporate_actions_client_warms_cookie_and_filters() -> None:
    def handler(m: str, url: str, kw: dict[str, Any]) -> FakeResponse:
        if url == nse.CA_PAGE_URL:
            return FakeResponse(200, b"<html>")
        assert kw["params"]["from_date"] == "01-01-2024"
        return FakeResponse(200, json_data=CA_RECORDS)

    sess = FakeSession(handler)
    client = NseCorporateActionsClient(session=sess)  # type: ignore[arg-type]
    df = client.fetch(date(2024, 1, 1), date(2024, 1, 31), ["TCS"])
    assert df["symbol"].tolist() == ["TCS"]
    assert sess.calls[0][1] == nse.CA_PAGE_URL
    client.fetch(date(2024, 1, 1), date(2024, 1, 31))
    assert sum(1 for c in sess.calls if c[1] == nse.CA_PAGE_URL) == 1


def test_corporate_actions_dict_payload() -> None:
    sess = FakeSession(lambda m, u, k: FakeResponse(200, json_data={"data": CA_RECORDS[:1]}))
    df = NseCorporateActionsClient(session=sess).fetch(date(2024, 1, 1), date(2024, 1, 31))  # type: ignore[arg-type]
    assert len(df) == 1


MASTER = (
    b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER,"
    b" FACE VALUE\n"
    b"RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    b"NESTLEIND,Nestle India Limited,EQ,08-JAN-2010,1,1,INE239A01024,1\n"
)


def test_parse_equity_master_and_client() -> None:
    df = parse_equity_master(MASTER, date(2026, 10, 21))
    assert df["symbol"].tolist() == ["RELIANCE", "NESTLEIND"]
    assert df.loc[0, "listing_date"] == pd.Timestamp("1995-11-29")
    assert df.loc[1, "face_value"] == 1.0
    sess = FakeSession(lambda m, u, k: FakeResponse(200, MASTER))
    got = NseSecuritiesClient(session=sess).fetch(symbols=["NESTLEIND"])  # type: ignore[arg-type]
    assert got["isin"].tolist() == ["INE239A01024"]


def test_fetch_holiday_names() -> None:
    payload = {"CM": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}, {}]}

    def handler(m: str, url: str, kw: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, json_data=payload) if "holiday" in url else FakeResponse(200)

    names = fetch_holiday_names(FakeSession(handler))  # type: ignore[arg-type]
    assert names == {date(2026, 1, 26): "Republic Day"}
    bad = FakeSession(lambda m, u, k: FakeResponse(403))
    assert fetch_holiday_names(bad) == {}  # type: ignore[arg-type]
