"""Orchestrates the source clients: `findata ingest <source|all> --from --to`.

Ingestion is idempotent. Each client's output is written by `write_partitions`, which replaces
whole date partitions, so re-running a range never duplicates rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from findata.calendar import NSE_CALENDAR_PATH
from findata.config import load_universe
from findata.ingest.base import write_partitions
from findata.ingest.fbil import FbilClient
from findata.ingest.fred import FredClient
from findata.ingest.gleif import GleifClient
from findata.ingest.nse import (
    NseBhavcopyClient,
    NseCorporateActionsClient,
    NseSecuritiesClient,
    fetch_holiday_names,
)
from findata.ingest.openfigi import OpenFigiClient
from findata.ingest.worldbank import WorldBankClient
from findata.ingest.yahoo import YahooClient
from findata.settings import Settings, get_settings
from findata.store import load_frame

SOURCES = ["nse", "yahoo", "fbil", "fred", "worldbank", "openfigi", "gleif"]
NO_SESSION = "No session (no bhavcopy published)"


def _write(df: pd.DataFrame, client: Any, settings: Settings) -> int:
    write_partitions(df, client.source, client.dataset, client.partition_col, settings)
    return len(df)


def build_calendar_yaml(
    start: date,
    end: date,
    available: list[date],
    names: dict[date, str] | None = None,
    path: Path = NSE_CALENDAR_PATH,
) -> dict[str, Any]:
    """Derive the NSE calendar from which days had a bhavcopy and write it to YAML.

    A weekday without a file is a holiday. A weekend day with a file is a special session
    (in 2024 NSE ran Saturday sessions, e.g. for disaster-recovery site drills).
    """
    names = names or {}
    have = set(available)
    holidays, specials = [], []
    for d in pd.date_range(start, end):
        day = d.date()
        if d.dayofweek < 5 and day not in have:
            holidays.append({"date": day.isoformat(), "name": names.get(day, NO_SESSION)})
        elif d.dayofweek >= 5 and day in have:
            specials.append({"date": day.isoformat(), "name": "Special weekend session"})
    spec: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": (
            "derived from NSE bhavcopy availability (nsearchives UDiFF files): a weekday "
            "with no file is a holiday, a weekend day with a file is a special session"
        ),
        "holidays": holidays,
        "special_sessions": specials,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(spec, fh, sort_keys=False)
    return spec


def ingest_nse(
    start: date, end: date, symbols: list[str], settings: Settings, write_calendar: bool = True
) -> dict[str, int]:
    out: dict[str, int] = {}
    sec = NseSecuritiesClient(settings=settings)
    out["nse_securities"] = _write(sec.fetch(), sec, settings)
    bhav = NseBhavcopyClient(settings=settings)
    out["nse_prices"] = _write(bhav.fetch(start, end, None), bhav, settings)
    if write_calendar and bhav.available_days:
        try:
            names = fetch_holiday_names(bhav.session)
        except Exception:  # names are a nicety; never fail the ingest for them
            names = {}
        build_calendar_yaml(start, end, bhav.available_days, names)
    ca = NseCorporateActionsClient(settings=settings)
    out["nse_corporate_actions"] = _write(ca.fetch(start, end, symbols), ca, settings)
    return out


def universe_isins(settings: Settings) -> tuple[list[str], dict[str, str]]:
    """ISINs to map: the current master ISIN plus every ISIN seen in prices for the universe.

    Returns:
        (isins, isin -> company name). Old ISINs inherit the company name of their symbol.
    """
    symbols = set(load_universe().symbols)
    master = load_frame("nse_securities", mode="raw", settings=settings)
    master = master[master["symbol"].isin(symbols)]
    company = dict(zip(master["symbol"], master["company"], strict=False))
    pairs: dict[str, str] = {
        str(i): str(s) for s, i in zip(master["symbol"], master["isin"], strict=False)
    }
    prices = load_frame("nse_prices", mode="raw", settings=settings)
    if not prices.empty:
        seen = prices[prices["symbol"].isin(symbols)][["symbol", "isin"]].drop_duplicates()
        pairs.update({str(i): str(s) for s, i in zip(seen["symbol"], seen["isin"], strict=False)})
    names = {i: company[s] for i, s in pairs.items() if s in company}
    return sorted(pairs), names


def _snapshot(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["snapshot"] = pd.Timestamp(date.today())
    return df


def run_ingest(
    source: str,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Run one source (or `all`) for [start, end]; returns rows written per dataset."""
    s = settings or get_settings()
    syms = symbols or load_universe().symbols
    wanted = SOURCES if source == "all" else [source]
    unknown = set(wanted) - set(SOURCES)
    if unknown:
        raise ValueError(f"Unknown source(s) {sorted(unknown)}; choose from {SOURCES} or 'all'")

    out: dict[str, int] = {}
    simple: dict[str, Callable[[], Any]] = {
        "fbil": lambda: FbilClient(settings=s),
        "fred": lambda: FredClient(settings=s),
        "worldbank": lambda: WorldBankClient(settings=s),
    }
    for src in wanted:
        if src == "nse":
            out.update(ingest_nse(start, end, syms, s))
        elif src == "yahoo":
            yc = YahooClient()
            out["yahoo_prices"] = _write(yc.fetch(start, end, syms), yc, s)
        elif src in simple:
            client = simple[src]()
            out[client.dataset] = _write(client.fetch(start, end, None), client, s)
        elif src == "openfigi":
            isins, _ = universe_isins(s)
            fc = OpenFigiClient(settings=s)
            out["openfigi_mapping"] = _write(_snapshot(fc.fetch(symbols=isins)), fc, s)
        elif src == "gleif":
            isins, names = universe_isins(s)
            gc = GleifClient(settings=s)
            out["gleif_lei"] = _write(_snapshot(gc.fetch(symbols=isins, names=names)), gc, s)
    return out
