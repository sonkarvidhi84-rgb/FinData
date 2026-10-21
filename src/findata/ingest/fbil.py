"""FBIL reference rates (the official INR fixing, formerly published by RBI).

Financial Benchmarks India Ltd took over the RBI reference rate in July 2018. It publishes
USD, GBP, EUR and JPY against INR around 13:30 IST on business days.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from findata.ingest.base import PoliteSession, utcnow
from findata.settings import Settings, get_settings

FBIL_URL = "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"
_PAIR_RE = re.compile(r"INR\s*/\s*(\d+)\s*([A-Z]{3})")


def parse_pair(sub_prod_name: str) -> tuple[str, float] | None:
    """`'INR / 100 JPY'` -> `('JPY', 100.0)`; None for anything else."""
    m = _PAIR_RE.search(sub_prod_name or "")
    if not m:
        return None
    return m.group(2), float(m.group(1))


def parse_fbil(records: list[dict[str, Any]], ingest_ts: pd.Timestamp) -> pd.DataFrame:
    """FBIL JSON records -> `fx_rates` shape. `displayTime` (IST) becomes `source_ts` (UTC)."""
    rows: list[dict[str, Any]] = []
    for r in records:
        pair = parse_pair(str(r.get("subProdName", "")))
        if pair is None or r.get("rate") is None:
            continue
        shown = pd.to_datetime(str(r.get("displayTime") or ""), errors="coerce")
        source_ts = None if pd.isna(shown) else shown.tz_localize("Asia/Kolkata").tz_convert("UTC")
        rows.append(
            {
                "rate_date": pd.Timestamp(str(r["processRunDate"])).normalize(),
                "base_ccy": pair[0],
                "quote_ccy": "INR",
                "units": pair[1],
                "rate": float(r["rate"]),
                "source_ts": source_ts,
                "ingest_ts": ingest_ts,
            }
        )
    cols = ["rate_date", "base_ccy", "quote_ccy", "units", "rate", "source_ts", "ingest_ts"]
    return pd.DataFrame(rows, columns=cols)


class FbilClient:
    """INR reference rates in chunks of at most 30 days per request."""

    source = "fbil"
    dataset = "fx_rates"
    partition_col = "rate_date"
    chunk_days = 30

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or PoliteSession(
            min_interval_s=max(self.settings.http_min_interval_s, 1.0),
            user_agent=self.settings.user_agent,
        )

    def fetch(
        self,
        start: date | datetime | pd.Timestamp,
        end: date | datetime | pd.Timestamp,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        frames: list[pd.DataFrame] = []
        cur = s
        while cur <= e:
            chunk_end = min(cur + timedelta(days=self.chunk_days - 1), e)
            params = {
                "fromDate": cur.isoformat(),
                "toDate": chunk_end.isoformat(),
                "authenticated": "false",
            }
            resp = self.session.get(FBIL_URL, params=params)
            resp.raise_for_status()
            frames.append(parse_fbil(resp.json(), utcnow()))
            cur = chunk_end + timedelta(days=1)
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(["rate_date", "base_ccy", "quote_ccy"]).reset_index(drop=True)
