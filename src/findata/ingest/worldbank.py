"""World Bank World Development Indicators for India (annual), via the public v2 REST API.

Like FRED, values are revised, so raw partitions are keyed by `vintage_date`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from findata.config import load_dataset
from findata.ingest.base import PoliteSession, utcnow
from findata.settings import Settings, get_settings

WB_URL = "https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"


def parse_worldbank(
    payload: Any, series_id: str, vintage: date, ingest_ts: pd.Timestamp
) -> pd.DataFrame:
    """WB JSON `[meta, [records...]]` -> `worldbank_macro` shape (obs_date = Jan 1 of year)."""
    cols = ["series_id", "obs_date", "value", "vintage_date", "ingest_ts"]
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return pd.DataFrame(columns=cols)
    rows = [
        {
            "series_id": series_id,
            "obs_date": pd.Timestamp(year=int(r["date"]), month=1, day=1),
            "value": None if r.get("value") is None else float(r["value"]),
            "vintage_date": pd.Timestamp(vintage),
            "ingest_ts": ingest_ts,
        }
        for r in payload[1]
        if str(r.get("date", "")).isdigit()
    ]
    return pd.DataFrame(rows, columns=cols)


class WorldBankClient:
    """Indicators listed under `series:` in `config/datasets/worldbank_macro.yaml`."""

    source = "worldbank"
    dataset = "worldbank_macro"
    partition_col = "vintage_date"

    def __init__(
        self,
        session: PoliteSession | None = None,
        settings: Settings | None = None,
        country: str = "IND",
        lookback_years: int = 10,
    ) -> None:
        self.settings = settings or get_settings()
        self.country = country
        self.lookback_years = lookback_years
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
        """Annual values from `lookback_years` before `start` to `end`.

        Annual data for the current year is rarely published yet, so a six-month window would
        return nothing. We therefore look back far enough to give analytics some context.
        `symbols` overrides the YAML series list.
        """
        series = symbols or load_dataset(self.dataset).series
        vintage = date.today()
        y0, y1 = pd.Timestamp(start).year - self.lookback_years, pd.Timestamp(end).year
        frames: list[pd.DataFrame] = []
        for sid in series:
            url = WB_URL.format(country=self.country, indicator=sid)
            params = {"format": "json", "date": f"{y0}:{y1}", "per_page": "100"}
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            frames.append(parse_worldbank(resp.json(), sid, vintage, utcnow()))
        return pd.concat(frames, ignore_index=True)
