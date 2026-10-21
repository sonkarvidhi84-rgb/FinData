"""NSE trading calendar loaded from `config/calendars/nse.yaml`.

Expected sessions = weekdays in [start, end] - holidays + special (weekend) sessions. Knowing
the expected sessions is what turns "rows we have" into a completeness percentage.

YAML format::

    start: 2024-01-01
    end: 2024-06-30
    source: <provenance text>
    holidays:          # weekdays with no session
      - {date: 2024-01-22, name: "..."}
    special_sessions:  # weekend days with a session
      - {date: 2024-01-20, name: "..."}
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from findata.config import CONFIG_DIR

NSE_CALENDAR_PATH = CONFIG_DIR / "calendars" / "nse.yaml"


def _dates(entries: list[dict[str, Any]] | None) -> dict[date, str]:
    return {pd.Timestamp(e["date"]).date(): str(e.get("name", "")) for e in entries or []}


def load_calendar_spec(path: Path = NSE_CALENDAR_PATH) -> dict[str, Any]:
    """Raw YAML mapping (empty dict if the calendar has not been generated yet)."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return dict(data)


def trading_days(
    start: date | None = None, end: date | None = None, path: Path = NSE_CALENDAR_PATH
) -> list[pd.Timestamp]:
    """Expected NSE sessions between start and end (defaults: calendar coverage)."""
    spec = load_calendar_spec(path)
    if not spec:
        return []
    lo = pd.Timestamp(start or spec["start"])
    hi = pd.Timestamp(end or spec["end"])
    lo = max(lo, pd.Timestamp(spec["start"]))
    hi = min(hi, pd.Timestamp(spec["end"]))
    holidays = _dates(spec.get("holidays"))
    specials = _dates(spec.get("special_sessions"))
    days = pd.bdate_range(lo, hi)
    out = {d for d in days if d.date() not in holidays}
    out |= {pd.Timestamp(d) for d in specials if lo.date() <= d <= hi.date()}
    return sorted(out)


def holiday_names(path: Path = NSE_CALENDAR_PATH) -> dict[date, str]:
    """Map of weekday holiday -> name."""
    return _dates(load_calendar_spec(path).get("holidays"))
