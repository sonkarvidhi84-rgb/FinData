"""Reading datasets in their canonical (YAML-typed) shape, from raw parquet or committed samples.

Canonical dtypes, the contract every downstream module relies on:

* `date` columns are tz-naive `datetime64` (unit s/ns under pandas 3) normalised to midnight.
* `datetime` columns are tz-aware UTC `datetime64[*, UTC]`.
* `float`/`int` columns are `float64` (nullable ints are awkward in pandas; keys are never numeric).
* `str` columns are pandas strings, with missing values as NA.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

from findata.config import DatasetConfig, load_dataset
from findata.ingest.base import read_partitions
from findata.settings import Settings, get_settings

InputMode = Literal["raw", "samples", "auto"]


def coerce(df: pd.DataFrame, cfg: DatasetConfig) -> pd.DataFrame:
    """Return a copy of `df` with exactly the YAML columns, in order, with canonical dtypes."""
    out = pd.DataFrame(index=df.index)
    for col in cfg.columns:
        s = df[col.name] if col.name in df.columns else pd.Series(pd.NA, index=df.index)
        if col.dtype == "date":
            out[col.name] = pd.to_datetime(s, errors="coerce").dt.tz_localize(None).dt.normalize()
            if isinstance(out[col.name].dtype, pd.DatetimeTZDtype):  # pragma: no cover
                out[col.name] = out[col.name].dt.tz_localize(None)
        elif col.dtype == "datetime":
            out[col.name] = pd.to_datetime(s, errors="coerce", utc=True)
        elif col.dtype in ("float", "int"):
            out[col.name] = pd.to_numeric(s, errors="coerce").astype("float64")
        else:
            out[col.name] = s.astype("string")
    return out.reset_index(drop=True)


def dedupe(df: pd.DataFrame, cfg: DatasetConfig) -> pd.DataFrame:
    """Keep the most recently ingested row per natural key (plus vintage, if versioned)."""
    if df.empty:
        return df
    key = list(cfg.natural_key)
    if "vintage_date" in df.columns:
        key.append("vintage_date")
    if "ingest_ts" in df.columns:
        df = df.sort_values("ingest_ts", kind="stable")
    return df.drop_duplicates(subset=key, keep="last").reset_index(drop=True)


def resolve_mode(
    mode: InputMode | str, settings: Settings | None = None
) -> Literal["raw", "samples"]:
    """`auto` means raw if any raw parquet exists, else the committed samples."""
    s = settings or get_settings()
    if mode == "raw":
        return "raw"
    if mode == "samples":
        return "samples"
    has_raw = any(s.raw_dir.glob("*/*/dt=*/part.parquet"))
    return "raw" if has_raw else "samples"


def sample_path(name: str, settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.samples_dir / f"{name}.csv"


def load_frame(
    name: str,
    mode: InputMode | str | None = None,
    settings: Settings | None = None,
    raw_dedupe: bool = True,
) -> pd.DataFrame:
    """Load dataset `name` in canonical shape.

    Args:
        name: Dataset name (YAML stem).
        mode: `raw`, `samples` or `auto`; defaults to `settings.input_mode`.
        settings: Override settings (tests).
        raw_dedupe: Drop duplicate natural keys. Validation passes False so that
            `duplicate_keys` can see them.
    """
    s = settings or get_settings()
    cfg = load_dataset(name)
    resolved = resolve_mode(mode or s.input_mode, s)
    if resolved == "raw":
        df = read_partitions(cfg.source, name, s)
    else:
        path = sample_path(name, s)
        df = pd.read_csv(path) if path.exists() else pd.DataFrame()
    df = coerce(df, cfg)
    return dedupe(df, cfg) if raw_dedupe else df


def write_staging(df: pd.DataFrame, name: str, settings: Settings | None = None) -> Path:
    """Write a validated dataset to `data/staging/<name>.parquet` (full replace, idempotent)."""
    s = settings or get_settings()
    s.staging_dir.mkdir(parents=True, exist_ok=True)
    path = s.staging_dir / f"{name}.parquet"
    df.to_parquet(path, index=False)
    return path


def read_staging(name: str, settings: Settings | None = None) -> pd.DataFrame:
    """Read a staged dataset; empty frame if it has not been validated yet."""
    s = settings or get_settings()
    path = s.staging_dir / f"{name}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()
