from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from findata.ingest import base
from findata.ingest.base import (
    PoliteSession,
    TransientHTTPError,
    read_partitions,
    utcnow,
    write_partitions,
)
from findata.settings import Settings


def _resp(code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = code
    return r


def test_throttle_sleeps_for_remaining_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([100.0, 100.2, 100.2])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(clock))
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    sess = MagicMock()
    sess.headers = {}
    sess.request.return_value = _resp(200)
    ps = PoliteSession(min_interval_s=1.0, user_agent="ua", session=sess)
    ps._throttle("https://example.com/a")
    ps._throttle("https://example.com/b")
    assert slept == [pytest.approx(0.8)]
    assert sess.headers["User-Agent"] == "ua"


def test_throttle_is_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base.time, "monotonic", lambda: 5.0)
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    sess = MagicMock()
    sess.headers = {}
    sess.request.return_value = _resp(200)
    ps = PoliteSession(min_interval_s=1.0, session=sess, headers={"X": "1"})
    ps.get("https://a.com/")
    ps.post("https://b.com/")
    assert slept == []
    assert sess.headers["X"] == "1"


def test_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PoliteSession.request.retry, "sleep", lambda s: None)  # type: ignore[attr-defined]
    sess = MagicMock()
    sess.headers = {}
    sess.request.side_effect = [_resp(503), _resp(429), _resp(200)]
    ps = PoliteSession(min_interval_s=0, session=sess)
    assert ps.get("https://x.com").status_code == 200
    assert sess.request.call_count == 3


def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PoliteSession.request.retry, "sleep", lambda s: None)  # type: ignore[attr-defined]
    sess = MagicMock()
    sess.headers = {}
    sess.request.return_value = _resp(500)
    ps = PoliteSession(min_interval_s=0, session=sess)
    with pytest.raises(TransientHTTPError):
        ps.get("https://x.com")


def test_404_is_returned_not_retried() -> None:
    sess = MagicMock()
    sess.headers = {}
    sess.request.return_value = _resp(404)
    ps = PoliteSession(min_interval_s=0, session=sess)
    assert ps.get("https://x.com").status_code == 404
    assert sess.request.call_count == 1


def test_write_partitions_is_idempotent(tmp_settings: Settings) -> None:
    df = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-02"]),
            "v": [1, 2, 3],
        }
    )
    paths = write_partitions(df, "src", "ds", "trade_date", tmp_settings)
    assert len(paths) == 2
    write_partitions(df, "src", "ds", "trade_date", tmp_settings)
    back = read_partitions("src", "ds", tmp_settings)
    assert len(back) == 3
    assert sorted(back["v"].tolist()) == [1, 2, 3]


def test_write_partitions_empty_and_read_missing(tmp_settings: Settings) -> None:
    assert write_partitions(pd.DataFrame(), "s", "d", "x", tmp_settings) == []
    assert read_partitions("s", "nothing", tmp_settings).empty


def test_utcnow_is_tz_aware() -> None:
    now: Any = utcnow()
    assert str(now.tz) == "UTC"
