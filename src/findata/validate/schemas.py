"""Pandera schemas generated from the dataset YAML contracts.

The YAML column list (dtype, nullability, `gt`/`ge`/`pattern`/`isin_set`) is turned into a
`DataFrameSchema`. This means the contract is written once and enforced everywhere. Schema
failures don't raise: they become `schema_validity` anomalies, and the offending rows are
dropped from staging.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from findata.config import ColumnDef, DatasetConfig
from findata.validate.anomaly import Anomaly

_DTYPES: dict[str, Any] = {
    "float": "float64",
    "int": "float64",
    "date": "datetime64[ns]",
    "datetime": "datetime64[ns, UTC]",
    "str": None,  # pandas 3 string dtypes vary (str / string); content is checked instead
}


def _checks(col: ColumnDef) -> list[pa.Check]:
    checks: list[pa.Check] = []
    if col.gt is not None:
        checks.append(pa.Check.gt(col.gt))
    if col.ge is not None:
        checks.append(pa.Check.ge(col.ge))
    if col.pattern is not None:
        checks.append(pa.Check.str_matches(col.pattern))
    if col.isin_set is not None:
        checks.append(pa.Check.isin(col.isin_set))
    return checks


def build_schema(cfg: DatasetConfig) -> pa.DataFrameSchema:
    """Build the pandera schema for a dataset contract.

    Natural-key uniqueness is enforced with `report_duplicates="exclude_first"`, so the first
    occurrence survives and later copies are reported and dropped.
    """
    columns = {
        c.name: pa.Column(
            _DTYPES[c.dtype],
            checks=_checks(c),
            nullable=c.nullable,
            coerce=_DTYPES[c.dtype] is not None,
            required=True,
        )
        for c in cfg.columns
    }
    key = list(cfg.natural_key)
    if "vintage_date" in cfg.column_names:
        key.append("vintage_date")
    return pa.DataFrameSchema(
        columns, unique=key, report_duplicates="exclude_first", name=cfg.name, strict=False
    )


def _fmt(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "NA"
    return str(value)


def record_key(row: pd.Series, cols: list[str]) -> str:
    """Join natural-key values with `|`, rendering dates as YYYY-MM-DD."""
    return "|".join(_fmt(row[c]) if c in row.index else "NA" for c in cols)


def validate_schema(df: pd.DataFrame, cfg: DatasetConfig) -> tuple[pd.DataFrame, list[Anomaly]]:
    """Validate `df` against the dataset contract.

    Returns:
        (valid_rows, anomalies). `valid_rows` keeps the original values of rows that passed every
        row-level check. A failure that isn't tied to a row (a missing column, for example)
        produces one anomaly and drops no rows.
    """
    df = df.reset_index(drop=True)
    schema = build_schema(cfg)
    try:
        schema.validate(df, lazy=True)
    except SchemaErrors as err:
        cases = err.failure_cases
    else:
        return df.reset_index(drop=True), []

    key_cols = list(cfg.natural_key)
    anomalies: list[Anomaly] = []
    bad_index: set[int] = set()
    seen: set[tuple[str, str]] = set()
    for case in cases.itertuples(index=False):
        idx = getattr(case, "index", None)
        column = str(getattr(case, "column", "") or "")
        check = str(getattr(case, "check", ""))
        failure = getattr(case, "failure_case", None)
        if idx is None or pd.isna(idx):
            rk = f"column:{column or 'dataframe'}"
            if (rk, check) in seen:
                continue
            seen.add((rk, check))
            anomalies.append(
                Anomaly(
                    dataset=cfg.name,
                    rule="schema_validity",
                    severity="high",
                    record_key=rk,
                    detail=f"Schema check '{check}' failed on column '{column}': {failure}",
                )
            )
            continue
        i = int(idx)
        bad_index.add(i)
        row = df.iloc[i]
        rk = record_key(row, key_cols)
        if (rk, f"{column}:{check}") in seen:
            continue
        seen.add((rk, f"{column}:{check}"))
        symbol = row.get("symbol", row.get("series_id", None))
        date_col = next(
            (c for c in ("trade_date", "rate_date", "obs_date", "ex_date") if c in row), None
        )
        anomalies.append(
            Anomaly(
                dataset=cfg.name,
                rule="schema_validity",
                severity="high" if "unique" not in check.lower() else "medium",
                record_key=rk,
                detail=f"Column '{column}' failed check '{check}' with value {failure!r}",
                symbol=None if symbol is None or pd.isna(symbol) else str(symbol),
                obs_date=_fmt(row[date_col]) if date_col else None,
            )
        )
    valid = df.drop(index=[i for i in bad_index if i in df.index])
    return valid.reset_index(drop=True), anomalies
