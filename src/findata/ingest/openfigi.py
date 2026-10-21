"""OpenFIGI: map ISINs to FIGIs (Financial Instrument Global Identifier).

FIGI is an open standard (OMG), originated by Bloomberg and free to use and redistribute. One ISIN
maps to several FIGIs, one per venue (IN = India composite, IS = NSE, IB = BSE, ...). We keep
the India composite listing because it is venue-neutral, which suits an identifier map.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from findata.ingest.base import PoliteSession, utcnow
from findata.settings import Settings, get_settings

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
BATCH_SIZE = 10  # max jobs per request without an API key
PREFERRED_EXCH = ("IN", "IS", "IB")

FIGI_COLUMNS = [
    "isin",
    "figi",
    "composite_figi",
    "share_class_figi",
    "ticker",
    "name",
    "security_type",
    "exch_code",
    "ingest_ts",
]


def pick_listing(data: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the India composite (exchCode IN), then NSE, then BSE, then the first row."""
    for code in PREFERRED_EXCH:
        for row in data:
            if row.get("exchCode") == code:
                return row
    return data[0] if data else None


def parse_openfigi(
    isins: list[str], results: list[dict[str, Any]], ingest_ts: pd.Timestamp
) -> pd.DataFrame:
    """Zip a batch of queried ISINs with the response jobs (same order) into rows."""
    rows: list[dict[str, Any]] = []
    for isin, job in zip(isins, results, strict=False):
        best = pick_listing(job.get("data") or [])
        if best is None:
            continue
        rows.append(
            {
                "isin": isin,
                "figi": best.get("figi"),
                "composite_figi": best.get("compositeFIGI"),
                "share_class_figi": best.get("shareClassFIGI"),
                "ticker": best.get("ticker"),
                "name": best.get("name"),
                "security_type": best.get("securityType"),
                "exch_code": best.get("exchCode"),
                "ingest_ts": ingest_ts,
            }
        )
    return pd.DataFrame(rows, columns=FIGI_COLUMNS)


class OpenFigiClient:
    """Batch ISIN -> FIGI mapping, ~25 requests/minute without a key."""

    source = "openfigi"
    dataset = "openfigi_mapping"
    partition_col = "snapshot"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        headers = {"Content-Type": "application/json"}
        if self.settings.openfigi_api_key:
            headers["X-OPENFIGI-APIKEY"] = self.settings.openfigi_api_key
        self.session = session or PoliteSession(
            min_interval_s=max(self.settings.http_min_interval_s, 2.5),
            user_agent=self.settings.user_agent,
            headers=headers,
        )

    def fetch(
        self,
        start: date | datetime | pd.Timestamp | None = None,
        end: date | datetime | pd.Timestamp | None = None,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """`symbols` here are ISINs. Dates are ignored (mapping is a snapshot)."""
        isins = sorted(set(symbols or []))
        frames: list[pd.DataFrame] = []
        for i in range(0, len(isins), BATCH_SIZE):
            batch = isins[i : i + BATCH_SIZE]
            jobs = [{"idType": "ID_ISIN", "idValue": x} for x in batch]
            resp = self.session.post(OPENFIGI_URL, json=jobs)
            resp.raise_for_status()
            frames.append(parse_openfigi(batch, resp.json(), utcnow()))
        if not frames:
            return pd.DataFrame(columns=FIGI_COLUMNS)
        return pd.concat(frames, ignore_index=True)
