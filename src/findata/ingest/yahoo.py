"""Yahoo Finance prices via the unofficial `yfinance` library: our second source.

We never treat Yahoo as authoritative. It exists so that `cross_source_price_diff` and the
accuracy metric have something independent to compare NSE against. Yahoo's `Close` is already
split-adjusted, so comparisons must put NSE prices on the same basis first.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

import pandas as pd

from findata.ingest.base import utcnow

Downloader = Callable[[str, date, date], pd.DataFrame]

YAHOO_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}


def _yf_download(ticker: str, start: date, end: date) -> pd.DataFrame:  # pragma: no cover
    """Call yfinance (network). `end` is exclusive in yfinance, so we add a day."""
    import yfinance as yf

    df = yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        multi_level_index=False,
    )
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def parse_yahoo(raw: pd.DataFrame, symbol: str, ingest_ts: pd.Timestamp) -> pd.DataFrame:
    """Reshape one ticker's yfinance frame (Date index, OHLCV columns) to `yahoo_prices`."""
    cols = ["trade_date", "symbol", *YAHOO_COLUMNS.values(), "ingest_ts"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=YAHOO_COLUMNS)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df = df.reset_index(names="trade_date")
    df["trade_date"] = df["trade_date"].dt.normalize()
    df["symbol"] = symbol
    df["ingest_ts"] = ingest_ts
    for c in YAHOO_COLUMNS.values():
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols]


class YahooClient:
    """Fetch daily OHLCV for NSE tickers from Yahoo (`<SYMBOL>.NS`)."""

    source = "yahoo"
    dataset = "yahoo_prices"
    partition_col = "trade_date"

    def __init__(self, downloader: Downloader | None = None, sleep_s: float = 1.5) -> None:
        self.downloader = downloader or _yf_download
        self.sleep_s = sleep_s

    def fetch(
        self,
        start: date | datetime | pd.Timestamp,
        end: date | datetime | pd.Timestamp,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """One request per ticker with a polite pause in between."""
        if not symbols:
            raise ValueError("YahooClient needs an explicit symbol list")
        s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        frames: list[pd.DataFrame] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(self.sleep_s)
            frames.append(parse_yahoo(self.downloader(f"{sym}.NS", s, e), sym, utcnow()))
        df = pd.concat(frames, ignore_index=True)
        mask = (df["trade_date"] >= pd.Timestamp(s)) & (df["trade_date"] <= pd.Timestamp(e))
        return df[mask].reset_index(drop=True)
