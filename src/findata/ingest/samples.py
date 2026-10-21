"""Build the committed offline samples (`data/samples/*.csv`) from raw data.

The samples are real data, truncated to the demo universe and date range, so the tests and the
dashboard run with no network (CLAUDE.md rule 6). Each file stays under 1 MB.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from findata.config import dataset_names, load_dataset, load_universe
from findata.settings import Settings, get_settings
from findata.store import load_frame, sample_path

MAX_BYTES = 1_000_000

# dataset -> (date column to window on, column holding a universe symbol)
_FILTERS: dict[str, tuple[str | None, str | None]] = {
    "nse_prices": ("trade_date", "symbol"),
    "yahoo_prices": ("trade_date", "symbol"),
    "nse_corporate_actions": ("ex_date", "symbol"),
    "nse_securities": (None, "symbol"),
    "fx_rates": ("rate_date", None),
    "fred_macro": ("obs_date", None),
    "worldbank_macro": (None, None),
    "openfigi_mapping": (None, None),
    "gleif_lei": (None, None),
}


def truncate(
    df: pd.DataFrame, name: str, start: date, end: date, symbols: list[str]
) -> pd.DataFrame:
    """Apply the universe and date-window filters for dataset `name`."""
    date_col, sym_col = _FILTERS.get(name, (None, None))
    if df.empty:
        return df
    if date_col:
        df = df[(df[date_col] >= pd.Timestamp(start)) & (df[date_col] <= pd.Timestamp(end))]
    if sym_col:
        df = df[df[sym_col].isin(symbols)]
    return df.reset_index(drop=True)


def build_samples(start: date, end: date, settings: Settings | None = None) -> dict[str, int]:
    """Write one CSV per dataset with raw data; returns rows written per dataset."""
    s = settings or get_settings()
    symbols = load_universe().symbols
    s.samples_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, int] = {}
    for name in dataset_names():
        df = load_frame(name, mode="raw", settings=s)
        if df.empty:
            continue
        df = truncate(df, name, start, end, symbols)
        key = [*load_dataset(name).natural_key]
        if "vintage_date" in df.columns:
            key.append("vintage_date")
        df = df.sort_values(key).reset_index(drop=True)
        path = sample_path(name, s)
        df.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"{path.name} is {size} bytes (> 1 MB); narrow the sample window")
        out[name] = len(df)
    return out
