"""Validation runner: schema check, then rules, then the staging and anomaly files.

For each dataset the engine:

1. loads it in canonical shape *without* de-duplication, so duplicates stay visible;
2. validates it against the pandera schema generated from YAML;
3. runs every rule enabled in the dataset YAML, with the context the rule needs;
4. writes the valid, de-duplicated rows to `data/staging/<dataset>.parquet`;
5. merges all anomalies into `data/staging/anomalies.parquet`.
"""

from __future__ import annotations

import pandas as pd

from findata.calendar import trading_days
from findata.config import DatasetConfig, dataset_names, load_dataset
from findata.settings import Settings, get_settings
from findata.store import dedupe, load_frame, write_staging
from findata.validate.anomaly import Anomaly, RuleContext, anomalies_to_frame
from findata.validate.rules import RULES
from findata.validate.schemas import validate_schema

# Context datasets are validated first so price rules can use their staged versions.
ORDER = [
    "nse_securities",
    "nse_corporate_actions",
    "yahoo_prices",
    "nse_prices",
    "fx_rates",
    "fred_macro",
    "worldbank_macro",
    "openfigi_mapping",
    "gleif_lei",
]

ANOMALIES_FILE = "anomalies.parquet"


def _ordered(dataset: str) -> list[str]:
    known = dataset_names()
    if dataset != "all":
        if dataset not in known:
            raise KeyError(f"Unknown dataset {dataset!r}; known: {', '.join(known)}")
        return [dataset]
    return [n for n in ORDER if n in known] + [n for n in known if n not in ORDER]


def split_vintages(df: pd.DataFrame, cfg: DatasetConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a versioned frame into (latest vintage per key, previous vintage per key)."""
    if df.empty or "vintage_date" not in df.columns:
        return df, df.iloc[0:0]
    ordered = df.sort_values("vintage_date", kind="stable")
    grp = ordered.groupby(cfg.natural_key, sort=False)
    latest = grp.tail(1)
    rank = grp.cumcount(ascending=False)
    previous = ordered[rank == 1]
    return latest.reset_index(drop=True), previous.reset_index(drop=True)


def validate_frame(
    df: pd.DataFrame,
    cfg: DatasetConfig,
    *,
    trading: list[pd.Timestamp] | None = None,
    corporate_actions: pd.DataFrame | None = None,
    secondary: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[Anomaly]]:
    """Validate one canonical frame; returns (staging rows, anomalies). Pure: no I/O."""
    anomalies: list[Anomaly] = []
    if df.empty:
        return df, anomalies
    valid, schema_anoms = validate_schema(df, cfg)
    anomalies.extend(schema_anoms)
    staged = dedupe(valid, cfg)
    latest, previous = split_vintages(staged, cfg)
    key = list(cfg.natural_key) + (["vintage_date"] if "vintage_date" in df.columns else [])
    for rule_name, params in cfg.rules.items():
        fn = RULES.get(rule_name)
        if fn is None:
            continue
        ctx = RuleContext(
            dataset=cfg.name,
            params={**params, "key": key},
            trading_days=trading or [],
            corporate_actions=corporate_actions,
            secondary=secondary,
            previous=previous,
        )
        # duplicate_keys must see the raw frame; everything else sees clean, latest rows.
        target = df if rule_name == "duplicate_keys" else latest
        anomalies.extend(fn(target, ctx))
    return staged, anomalies


def run_validation(
    dataset: str = "all", mode: str = "auto", settings: Settings | None = None
) -> dict[str, dict[str, int]]:
    """Validate one or all datasets and write staging + anomalies.

    Args:
        dataset: Dataset name or `all`.
        mode: `raw`, `samples` or `auto`.
        settings: Override settings (tests).

    Returns:
        `{dataset: {"rows": staged_rows, "anomalies": n}}`.
    """
    s = settings or get_settings()
    names = _ordered(dataset)
    trading = trading_days()
    staged: dict[str, pd.DataFrame] = {}
    summary: dict[str, dict[str, int]] = {}
    all_anoms: list[Anomaly] = []

    def context_frame(name: str) -> pd.DataFrame:
        if name not in staged:
            staged[name] = load_frame(name, mode, s)
        return staged[name]

    for name in names:
        cfg = load_dataset(name)
        df = load_frame(name, mode, s, raw_dedupe=False)
        needs_prices_ctx = name == "nse_prices"
        out, anoms = validate_frame(
            df,
            cfg,
            trading=trading,
            corporate_actions=context_frame("nse_corporate_actions") if needs_prices_ctx else None,
            secondary=context_frame("yahoo_prices") if needs_prices_ctx else None,
        )
        staged[name] = out
        write_staging(out, name, s)
        all_anoms.extend(anoms)
        summary[name] = {"rows": len(out), "anomalies": len(anoms)}

    new = anomalies_to_frame(all_anoms)
    path = s.staging_dir / ANOMALIES_FILE
    if dataset != "all" and path.exists():
        old = pd.read_parquet(path)
        old = old[~old["dataset"].isin(names)]
        new = pd.concat([old, new], ignore_index=True) if not old.empty else new
    s.staging_dir.mkdir(parents=True, exist_ok=True)
    new.astype({"value": "float64"}).to_parquet(path, index=False)
    return summary
