"""FRED (Federal Reserve Bank of St. Louis) macro series via the keyless CSV endpoint.

Macro data gets revised. CPI, for instance, is re-seasonally-adjusted every year. So every
fetch is stamped with `vintage_date` = the day we observed it, and raw partitions are keyed
by vintage. A later fetch adds a new vintage and never overwrites the old one, which is
what lets `macro_revision_detected` see revisions.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pandas as pd
import requests

from findata.config import load_dataset
from findata.ingest.base import PoliteSession, utcnow
from findata.settings import Settings, get_settings

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fred_user_agent(project_ua: str) -> str:
    """User-Agent for FRED: the library token first, then our project identity.

    FRED's edge firewall stalls requests whose User-Agent does not start with a recognised
    client token. A bare project name timed out for 15-90 s in testing, while
    `python-requests/x <project>` answered in under a second. So we keep the descriptive
    identity and put the library token in front of it.
    """
    return f"python-requests/{requests.__version__} {project_ua}"


def parse_fred_csv(
    csv_bytes: bytes, series_id: str, vintage: date, ingest_ts: pd.Timestamp
) -> pd.DataFrame:
    """fredgraph CSV (`observation_date,<SERIES>`) -> `fred_macro` shape. "." becomes null."""
    raw = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    date_col = raw.columns[0]
    val_col = series_id if series_id in raw.columns else raw.columns[-1]
    return pd.DataFrame(
        {
            "series_id": series_id,
            "obs_date": pd.to_datetime(raw[date_col]),
            "value": pd.to_numeric(raw[val_col].replace(".", pd.NA), errors="coerce"),
            "vintage_date": pd.Timestamp(vintage),
            "ingest_ts": ingest_ts,
        }
    )


class FredClient:
    """Series listed under `series:` in `config/datasets/fred_macro.yaml`."""

    source = "fred"
    dataset = "fred_macro"
    partition_col = "vintage_date"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or PoliteSession(
            min_interval_s=max(self.settings.http_min_interval_s, 1.0),
            user_agent=fred_user_agent(self.settings.user_agent),
        )

    def fetch(
        self,
        start: date | datetime | pd.Timestamp,
        end: date | datetime | pd.Timestamp,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """`symbols` overrides the YAML series list (FRED codes)."""
        series = symbols or load_dataset(self.dataset).series
        vintage = date.today()
        frames: list[pd.DataFrame] = []
        for sid in series:
            params = {
                "id": sid,
                "cosd": pd.Timestamp(start).date().isoformat(),
                "coed": pd.Timestamp(end).date().isoformat(),
            }
            resp = self.session.get(FRED_CSV_URL, params=params, timeout=90)
            resp.raise_for_status()
            frames.append(parse_fred_csv(resp.content, sid, vintage, utcnow()))
        return pd.concat(frames, ignore_index=True)
