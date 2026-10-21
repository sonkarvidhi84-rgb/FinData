from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select, text, update

from findata.model import loaders
from findata.model import tables as t
from findata.model.db import get_engine, upgrade
from findata.model.loaders import load_frames, run_load
from findata.model.reference import calendar_rows
from findata.settings import Settings

from .conftest import NESTLE_NEW, NESTLE_OLD, SYMBOLS, anomalies_frame


def test_run_load_end_to_end_and_idempotent(staged_settings: Settings) -> None:
    first = run_load("samples", staged_settings)
    second = run_load("samples", staged_settings)
    assert first == second
    assert first["dim_security"] == 20  # whole universe as dimension rows
    assert first["fact_price_daily"] == 2 * len(SYMBOLS) * 23  # nse + yahoo, Jan 2024 weekdays
    assert first["fact_corporate_action"] == 2  # out-of-universe action dropped
    assert first["fact_adjustment_factor"] == 2
    assert first["dq_anomaly"] == 2
    assert first["fact_macro_series"] > 20
    assert first["fact_fx_rate"] == 46
    assert first["dim_issuer"] == 1


def test_isin_history_and_adjusted_view(staged_settings: Settings) -> None:
    run_load("samples", staged_settings)
    engine = get_engine(staged_settings.db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.id_value, m.valid_from, m.valid_to FROM map_security_identifier m "
                "JOIN dim_security s USING (security_id) "
                "WHERE s.symbol='NESTLEIND' AND m.id_type='ISIN' ORDER BY m.valid_from"
            )
        ).all()
        assert [r[0] for r in rows] == [NESTLE_OLD, NESTLE_NEW]
        assert str(rows[0][2]) == "2024-01-04" and rows[1][2] is None
        types = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT id_type FROM map_security_identifier m "
                    "JOIN dim_security s USING (security_id) WHERE s.symbol='RELIANCE'"
                )
            )
        }
        assert types == {"ISIN", "TICKER", "FIGI", "COMPOSITE_FIGI", "LEI"}
        adj = conn.execute(
            text(
                "SELECT trade_date, close, adjusted_close FROM v_price_adjusted "
                "WHERE symbol='NESTLEIND' AND trade_date IN ('2024-01-04','2024-01-05') "
                "ORDER BY trade_date"
            )
        ).all()
        assert adj[0][2] == pytest.approx(adj[0][1] * 0.1)
        assert adj[1][2] == pytest.approx(adj[1][1])
        issuer = conn.execute(
            text(
                "SELECT i.legal_name FROM dim_security s JOIN dim_issuer i USING (issuer_id) "
                "WHERE s.symbol='RELIANCE'"
            )
        ).scalar_one()
        assert issuer == "RELIANCE INDUSTRIES LIMITED"
    engine.dispose()


def test_anomaly_status_survives_reload(staged_settings: Settings) -> None:
    run_load("samples", staged_settings)
    engine = get_engine(staged_settings.db_url)
    with engine.begin() as conn:
        conn.execute(update(t.dq_anomaly).values(status="reviewed"))
        first_seen = conn.execute(select(t.dq_anomaly.c.first_seen_ts)).scalars().all()
    changed = anomalies_frame()
    changed.loc[0, "detail"] = "updated detail"
    with engine.begin() as conn:
        loaders.load_anomalies(conn, changed)
    with engine.connect() as conn:
        rows = conn.execute(
            select(t.dq_anomaly.c.status, t.dq_anomaly.c.detail, t.dq_anomaly.c.first_seen_ts)
        ).all()
    assert {r[0] for r in rows} == {"reviewed"}
    assert "updated detail" in {r[1] for r in rows}
    assert [r[2] for r in rows] == first_seen
    engine.dispose()


def test_load_frames_with_empty_inputs(tmp_path: object, staged_settings: Settings) -> None:
    upgrade(staged_settings.db_url)
    engine = get_engine(staged_settings.db_url)
    counts = load_frames(engine, {}, None, symbols=["RELIANCE"])
    assert counts["dim_security"] == 1
    assert counts["fact_price_daily"] == 0
    assert loaders.load_anomalies.__name__  # smoke
    with engine.begin() as conn:
        assert loaders.load_anomalies(conn, pd.DataFrame()) == 0
    engine.dispose()


def test_validation_triggered_when_staging_missing(
    tmp_path: object, staged_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    (staged_settings.staging_dir / "yahoo_prices.parquet").unlink()
    calls: list[tuple[str, str]] = []

    import sys
    import types

    fake = types.ModuleType("findata.validate.engine")

    def run_validation(dataset: str, mode: str) -> dict[str, dict[str, int]]:
        calls.append((dataset, mode))
        return {}

    fake.run_validation = run_validation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "findata.validate.engine", fake)
    frames = loaders.load_staging_frames(staged_settings, "samples")
    assert calls == [("all", "samples")]
    assert frames["yahoo_prices"].empty


def test_helpers() -> None:
    assert loaders._as_date(None) is None
    assert loaders._as_date(date(2024, 1, 1)) == date(2024, 1, 1)
    assert loaders._as_ts(pd.Timestamp("2024-01-01")).tzinfo is not None  # type: ignore[union-attr]
    assert loaders._as_float(float("nan")) is None
    assert loaders._as_str(pd.NA) is None


def test_calendar_rows_explicit_range() -> None:
    rows = calendar_rows(date(2024, 1, 5), date(2024, 1, 8))
    assert [r["date_id"] for r in rows] == [20240105, 20240106, 20240107, 20240108]
    assert [r["is_weekend"] for r in rows] == [False, True, True, False]
    assert rows[1]["is_nse_trading_day"] is False
