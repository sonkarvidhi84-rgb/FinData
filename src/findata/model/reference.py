"""Static reference data: exchanges, currencies, macro-series catalogue, calendar dimension."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from findata.calendar import holiday_names, load_calendar_spec, trading_days

EXCHANGES: list[dict[str, Any]] = [
    {
        "mic": "XNSE",
        "name": "National Stock Exchange of India",
        "country": "IN",
        "timezone": "Asia/Kolkata",
        "currency_code": "INR",
    },
]

CURRENCIES: list[dict[str, Any]] = [
    {"code": "INR", "name": "Indian Rupee"},
    {"code": "USD", "name": "US Dollar"},
    {"code": "EUR", "name": "Euro"},
    {"code": "GBP", "name": "Pound Sterling"},
    {"code": "JPY", "name": "Japanese Yen"},
]

MACRO_SERIES: list[dict[str, Any]] = [
    {
        "source": "fred",
        "code": "CPIAUCSL",
        "name": "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
        "frequency": "M",
        "units": "Index 1982-1984=100, SA",
    },
    {
        "source": "fred",
        "code": "DFF",
        "name": "Federal Funds Effective Rate",
        "frequency": "D",
        "units": "Percent",
    },
    {
        "source": "fred",
        "code": "DGS10",
        "name": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
        "frequency": "D",
        "units": "Percent",
    },
    {
        "source": "fred",
        "code": "VIXCLS",
        "name": "CBOE Volatility Index: VIX",
        "frequency": "D",
        "units": "Index",
    },
    {
        "source": "fred",
        "code": "DEXINUS",
        "name": "Indian Rupees to U.S. Dollar Spot Exchange Rate (H.10)",
        "frequency": "D",
        "units": "INR per USD",
    },
    {
        "source": "worldbank",
        "code": "NY.GDP.MKTP.CD",
        "name": "India GDP (current US$)",
        "frequency": "A",
        "units": "US$",
    },
    {
        "source": "worldbank",
        "code": "FP.CPI.TOTL.ZG",
        "name": "India inflation, consumer prices (annual %)",
        "frequency": "A",
        "units": "Percent",
    },
    {
        "source": "worldbank",
        "code": "SP.POP.TOTL",
        "name": "India population, total",
        "frequency": "A",
        "units": "Persons",
    },
]


def calendar_rows(start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    """Rows for `dim_calendar` covering [start, end].

    Defaults to the coverage of `config/calendars/nse.yaml`. Without a calendar file every
    weekday is treated as a session. That is an assumption, and the completeness metric will
    say so via missing holidays.
    """
    spec = load_calendar_spec()
    lo = start or (pd.Timestamp(spec["start"]).date() if spec else None)
    hi = end or (pd.Timestamp(spec["end"]).date() if spec else None)
    if lo is None or hi is None:
        return []
    sessions = {d.date() for d in trading_days(lo, hi)} if spec else set()
    holidays = holiday_names() if spec else {}
    rows: list[dict[str, Any]] = []
    d = lo
    while d <= hi:
        is_weekend = d.weekday() >= 5
        is_session = d in sessions if spec else not is_weekend
        rows.append(
            {
                "date_id": d.year * 10000 + d.month * 100 + d.day,
                "cal_date": d,
                "year": d.year,
                "quarter": (d.month - 1) // 3 + 1,
                "month": d.month,
                "day_of_week": d.weekday(),
                "is_weekend": is_weekend,
                "is_nse_trading_day": is_session,
                "holiday_name": holidays.get(d) if not is_session and not is_weekend else None,
            }
        )
        d += timedelta(days=1)
    return rows
