"""Shared ingestion plumbing: a polite HTTP session and idempotent parquet partitions.

Every source client goes through `PoliteSession`, which enforces a minimum interval per host,
sends a descriptive User-Agent and retries transient failures with exponential back-off. This
keeps the pipeline within the terms of public sources (CLAUDE.md rule 7).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from findata.settings import Settings, get_settings

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TransientHTTPError(Exception):
    """A 429/5xx response that is worth retrying."""


def utcnow() -> pd.Timestamp:
    """Current time as a tz-aware UTC timestamp."""
    return pd.Timestamp(datetime.now(UTC))


class PoliteSession:
    """`requests.Session` wrapper with per-host rate limiting and retries.

    Args:
        min_interval_s: Minimum seconds between two requests to the same host.
        user_agent: Header sent with every request.
        headers: Extra default headers.
        session: Injected session (tests pass a mock).
    """

    def __init__(
        self,
        min_interval_s: float = 1.0,
        user_agent: str | None = None,
        headers: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.min_interval_s = min_interval_s
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent or get_settings().user_agent
        if headers:
            self.session.headers.update(headers)
        self._last_call: dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    @retry(
        retry=retry_if_exception_type(
            (TransientHTTPError, requests.ConnectionError, requests.Timeout)
        ),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send a request; retry on 429/5xx and network errors, return other responses as-is."""
        self._throttle(url)
        kwargs.setdefault("timeout", 30)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientHTTPError(f"{resp.status_code} from {url}")
        return resp

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)


class Client(Protocol):
    """Interface every source client implements (AGENT.md Phase 1)."""

    source: str
    dataset: str
    partition_col: str

    def fetch(
        self,
        start: datetime | pd.Timestamp,
        end: datetime | pd.Timestamp,
        symbols: list[str] | None,
    ) -> pd.DataFrame: ...


def raw_dataset_dir(source: str, dataset: str, settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.raw_dir / source / dataset


def write_partitions(
    df: pd.DataFrame,
    source: str,
    dataset: str,
    partition_col: str,
    settings: Settings | None = None,
) -> list[Path]:
    """Write `df` as `data/raw/<source>/<dataset>/dt=<YYYY-MM-DD>/part.parquet`.

    Each partition file is fully replaced, so re-running an ingest for the same dates
    overwrites rather than appends. That is what makes ingestion idempotent (rule 2).
    """
    if df.empty:
        return []
    base = raw_dataset_dir(source, dataset, settings)
    keys = pd.to_datetime(df[partition_col]).dt.strftime("%Y-%m-%d")
    written: list[Path] = []
    for key, part in df.groupby(keys, sort=True):
        out = base / f"dt={key}"
        out.mkdir(parents=True, exist_ok=True)
        path = out / "part.parquet"
        part.reset_index(drop=True).to_parquet(path, index=False)
        written.append(path)
    return written


def read_partitions(source: str, dataset: str, settings: Settings | None = None) -> pd.DataFrame:
    """Concatenate every partition of a raw dataset (empty frame if none)."""
    base = raw_dataset_dir(source, dataset, settings)
    files = sorted(base.glob("dt=*/part.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
