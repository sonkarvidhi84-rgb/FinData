"""Typed access to `config/datasets/*.yaml` and `config/universe.yaml`.

The dataset YAML is the single contract for a dataset: it drives the pandera schema, the rule
parameters, the quality thresholds and the data dictionary (CLAUDE.md rule 3).
"""

from __future__ import annotations

from functools import cache, lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from findata.settings import PROJECT_ROOT

CONFIG_DIR = PROJECT_ROOT / "config"
DATASETS_DIR = CONFIG_DIR / "datasets"

DType = Literal["str", "float", "int", "date", "datetime"]


class ColumnDef(BaseModel):
    """One column of a dataset contract."""

    name: str
    dtype: DType
    nullable: bool = True
    description: str = ""
    pattern: str | None = None
    gt: float | None = None
    ge: float | None = None
    isin_set: list[str] | None = None


class DatasetConfig(BaseModel):
    """A dataset contract as declared in YAML."""

    name: str
    description: str
    source: str
    source_url: str
    schedule: str
    grain: str
    natural_key: list[str]
    columns: list[ColumnDef]
    quality: dict[str, float] = Field(default_factory=dict)
    rules: dict[str, dict[str, Any]] = Field(default_factory=dict)
    series: list[str] = Field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def rule_params(self, rule: str) -> dict[str, Any]:
        """Parameters for `rule`, or an empty dict if the rule is not enabled."""
        return dict(self.rules.get(rule, {}))


class UniverseMember(BaseModel):
    symbol: str
    sector: str


class Universe(BaseModel):
    exchange: str
    securities: list[UniverseMember]

    @property
    def symbols(self) -> list[str]:
        return [s.symbol for s in self.securities]


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


@cache
def load_dataset(name: str, config_dir: Path = DATASETS_DIR) -> DatasetConfig:
    """Load and validate one dataset contract by name."""
    path = config_dir / f"{name}.yaml"
    if not path.exists():
        raise KeyError(f"No dataset definition at {path} (rule 3: YAML before code)")
    return DatasetConfig.model_validate(_read_yaml(path))


def dataset_names(config_dir: Path = DATASETS_DIR) -> list[str]:
    """All dataset names with a YAML contract, sorted."""
    return sorted(p.stem for p in config_dir.glob("*.yaml"))


@lru_cache(maxsize=1)
def load_universe(path: Path = CONFIG_DIR / "universe.yaml") -> Universe:
    """The demo security universe (symbols + sector)."""
    return Universe.model_validate(_read_yaml(path))
