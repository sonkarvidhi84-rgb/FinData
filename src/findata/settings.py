"""Runtime settings, read from environment variables and an optional `.env` file.

Secrets (API keys) only ever live in the environment, never in code or YAML.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed view over the process environment.

    Attributes:
        data_dir: Root for `raw/`, `staging/` and `samples/`.
        database_url: SQLAlchemy URL. Empty means local SQLite at `data/findata.db`.
        contact_email: Appended to the User-Agent so source operators can reach us.
        fred_api_key: Optional; the keyless CSV endpoint is used when absent.
        openfigi_api_key: Optional; raises the OpenFIGI rate limit.
        http_min_interval_s: Minimum seconds between two requests to the same host.
        input_mode: `raw`, `samples` or `auto` (raw if present, else samples).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    database_url: str = ""
    contact_email: str = ""
    fred_api_key: str = ""
    openfigi_api_key: str = ""
    http_min_interval_s: float = 1.0
    input_mode: str = "auto"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL, defaulting to a SQLite file under `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'findata.db').as_posix()}"

    @property
    def user_agent(self) -> str:
        contact = f" (+{self.contact_email})" if self.contact_email else ""
        return f"FinDataQualityPipeline/0.1 research project{contact}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (tests call `get_settings.cache_clear()`)."""
    return Settings()
