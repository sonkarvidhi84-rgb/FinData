"""Staging → warehouse loaders: dimensions first (upsert), then facts (upsert on natural key).

Every write is an upsert on the natural key enforced by a unique constraint, so `findata load`
can be re-run any number of times without creating duplicates (CLAUDE.md rule 2). The scope of
the warehouse is the demo universe (`config/universe.yaml`); raw data for other symbols stays in
`data/raw` and `data/staging`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import Engine, Table, delete, func, select
from sqlalchemy.engine import Connection

from findata.config import load_universe
from findata.model import tables as t
from findata.model.adjustments import build_segments, event_factors, records
from findata.model.db import get_engine, upgrade, upsert
from findata.model.reference import CURRENCIES, EXCHANGES, MACRO_SERIES, calendar_rows
from findata.settings import Settings, get_settings
from findata.store import read_staging

CORE_DATASETS = ("nse_prices", "nse_corporate_actions", "yahoo_prices")
ALL_DATASETS = (
    "nse_prices",
    "nse_corporate_actions",
    "nse_securities",
    "yahoo_prices",
    "fx_rates",
    "fred_macro",
    "worldbank_macro",
    "openfigi_mapping",
    "gleif_lei",
)
MACRO_SOURCES = {"fred_macro": "fred", "worldbank_macro": "worldbank"}


# --------------------------------------------------------------------------- conversions


def _is_missing(v: Any) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    return isinstance(v, float) and math.isnan(v)


def _as_date(v: Any) -> date | None:
    if _is_missing(v):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    return pd.Timestamp(v).date()


def _as_ts(v: Any) -> datetime | None:
    if _is_missing(v):
        return None
    ts = pd.Timestamp(v)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").to_pydatetime()


def _as_float(v: Any) -> float | None:
    if _is_missing(v):
        return None
    return float(v)


def _as_str(v: Any) -> str | None:
    if _is_missing(v):
        return None
    return str(v)


def _now() -> datetime:
    return pd.Timestamp.now(tz="UTC").to_pydatetime()


def _count(conn: Connection, table: Table) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


# --------------------------------------------------------------------------- inputs


def load_staging_frames(settings: Settings, mode: str = "auto") -> dict[str, pd.DataFrame]:
    """Read every staged dataset, running validation first if core ones are missing."""
    missing = [d for d in CORE_DATASETS if not (settings.staging_dir / f"{d}.parquet").exists()]
    if missing:
        from findata.validate.engine import run_validation

        run_validation("all", mode)
    return {name: read_staging(name, settings) for name in ALL_DATASETS}


def _read_anomalies(settings: Settings) -> pd.DataFrame:
    path = settings.staging_dir / "anomalies.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


# --------------------------------------------------------------------------- loaders


def _seed_reference(conn: Connection, prices: pd.DataFrame) -> None:
    upsert(conn, t.dim_exchange, EXCHANGES, ["mic"])
    upsert(conn, t.dim_currency, CURRENCIES, ["code"])
    upsert(conn, t.dim_macro_series, MACRO_SERIES, ["source", "code"])
    rows = calendar_rows()
    if not rows and not prices.empty:
        dates = pd.to_datetime(prices["trade_date"])
        rows = calendar_rows(dates.min().date(), dates.max().date())
    upsert(conn, t.dim_calendar, rows, ["date_id"])


def _id_map(conn: Connection, table: Table, key: str, value: str) -> dict[Any, int]:
    return {row[0]: int(row[1]) for row in conn.execute(select(table.c[key], table.c[value])).all()}


def _load_issuers(conn: Connection, gleif: pd.DataFrame, now: datetime) -> dict[str, int]:
    """Upsert dim_issuer from GLEIF; return ISIN -> issuer_id."""
    if gleif.empty:
        return {}
    rows = [
        {
            "lei": r["lei"],
            "legal_name": _as_str(r["legal_name"]) or "",
            "country": (_as_str(r["country"]) or "")[:2] or None,
            "entity_status": _as_str(r["entity_status"]),
            "parent_lei": _as_str(r["parent_lei"]),
            "load_ts": now,
        }
        for r in records(gleif)
        if not _is_missing(r.get("lei"))
    ]
    upsert(conn, t.dim_issuer, rows, ["lei"])
    lei_to_id = _id_map(conn, t.dim_issuer, "lei", "issuer_id")
    return {
        str(r["isin"]): lei_to_id[r["lei"]]
        for r in records(gleif)
        if not _is_missing(r.get("lei")) and r["lei"] in lei_to_id
    }


def _current_isins(prices: pd.DataFrame, master: pd.DataFrame) -> dict[str, str]:
    """Symbol -> most recently observed ISIN (prices win over the master snapshot)."""
    out: dict[str, str] = {}
    if not master.empty:
        out.update({str(r["symbol"]): str(r["isin"]) for r in records(master)})
    if not prices.empty:
        last = prices.sort_values("trade_date").groupby("symbol").tail(1)
        out.update({str(r["symbol"]): str(r["isin"]) for r in records(last)})
    return out


def _load_securities(
    conn: Connection,
    frames: dict[str, pd.DataFrame],
    symbols: list[str],
    sectors: dict[str, str],
    now: datetime,
) -> dict[str, int]:
    """Upsert dim_security (+ dim_issuer); return symbol -> security_id."""
    prices = frames["nse_prices"]
    master = frames["nse_securities"]
    figi = frames["openfigi_mapping"]
    exchange_id = _id_map(conn, t.dim_exchange, "mic", "exchange_id")["XNSE"]
    isin_to_issuer = _load_issuers(conn, frames["gleif_lei"], now)
    current_isin = _current_isins(prices, master)
    master_by_symbol = {str(r["symbol"]): r for r in records(master)} if not master.empty else {}
    figi_by_isin = {str(r["isin"]): r for r in records(figi)} if not figi.empty else {}
    names_from_prices: dict[str, str] = {}
    if not prices.empty and "name" in prices:
        last = prices.dropna(subset=["name"]).sort_values("trade_date").groupby("symbol").tail(1)
        names_from_prices = {str(r["symbol"]): str(r["name"]) for r in records(last)}

    rows = []
    for sym in symbols:
        m = master_by_symbol.get(sym, {})
        isin = current_isin.get(sym)
        f = figi_by_isin.get(isin or "", {})
        rows.append(
            {
                "exchange_id": exchange_id,
                "symbol": sym,
                "name": _as_str(m.get("company")) or names_from_prices.get(sym),
                "sector": sectors.get(sym),
                "security_type": _as_str(f.get("security_type")),
                "issuer_id": isin_to_issuer.get(isin or ""),
                "face_value": _as_float(m.get("face_value")),
                "listing_date": _as_date(m.get("listing_date")),
                "load_ts": now,
            }
        )
    upsert(conn, t.dim_security, rows, ["exchange_id", "symbol"])
    return _id_map(conn, t.dim_security, "symbol", "security_id")


def _identifier_rows(
    frames: dict[str, pd.DataFrame], sec_ids: dict[str, int], now: datetime
) -> list[dict[str, Any]]:
    """Build map_security_identifier rows with validity windows (SCD type 2)."""
    prices = frames["nse_prices"]
    rows: list[dict[str, Any]] = []

    def add(sym: str, id_type: str, value: Any, source: str, lo: Any, hi: Any) -> None:
        if sym not in sec_ids or _is_missing(value) or _as_date(lo) is None:
            return
        rows.append(
            {
                "security_id": sec_ids[sym],
                "id_type": id_type,
                "id_value": str(value),
                "source": source,
                "valid_from": _as_date(lo),
                "valid_to": _as_date(hi),
                "load_ts": now,
            }
        )

    first_seen: dict[str, Any] = {}
    isin_to_symbol: dict[str, str] = {}
    isin_from: dict[str, Any] = {}
    if not prices.empty:
        win = (
            prices.groupby(["symbol", "isin"], as_index=False)["trade_date"]
            .agg(["min", "max"])
            .reset_index(drop=True)
        )
        for sym in sorted(str(x) for x in win["symbol"].unique()):
            windows = records(win[win["symbol"] == sym].sort_values("max"))
            first_seen[sym] = min(w["min"] for w in windows)
            for i, w in enumerate(windows):
                current = i == len(windows) - 1
                add(sym, "ISIN", w["isin"], "nse", w["min"], None if current else w["max"])
                isin_to_symbol[str(w["isin"])] = sym
                isin_from[str(w["isin"])] = w["min"]
            add(sym, "TICKER", sym, "nse", first_seen[sym], None)

    master = frames["nse_securities"]
    if not master.empty:
        for r in records(master):
            msym, misin = str(r["symbol"]), str(r["isin"])
            if misin not in isin_to_symbol and msym in sec_ids:
                lo = first_seen.get(msym, r.get("snapshot_date"))
                add(msym, "ISIN", misin, "nse_master", lo, None)
                isin_to_symbol[misin] = msym
                isin_from[misin] = lo
                if msym not in first_seen:
                    add(msym, "TICKER", msym, "nse_master", lo, None)

    figi = frames["openfigi_mapping"]
    if not figi.empty:
        for r in records(figi):
            fisin = str(r["isin"])
            fsym = isin_to_symbol.get(fisin)
            if fsym is None:
                continue
            lo = isin_from.get(fisin)
            add(fsym, "FIGI", r.get("figi"), "openfigi", lo, None)
            add(fsym, "COMPOSITE_FIGI", r.get("composite_figi"), "openfigi", lo, None)

    gleif = frames["gleif_lei"]
    if not gleif.empty:
        for r in records(gleif):
            gsym = isin_to_symbol.get(str(r["isin"]))
            if gsym is not None:
                add(gsym, "LEI", r.get("lei"), "gleif", first_seen.get(gsym), None)
    return rows


def _price_rows(
    df: pd.DataFrame, source: str, sec_ids: dict[str, int], now: datetime
) -> list[dict[str, Any]]:
    if df.empty:
        return []
    df = df[df["symbol"].isin(list(sec_ids))].sort_values("trade_date")
    df = df.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    rows = []
    for r in records(df):
        rows.append(
            {
                "security_id": sec_ids[str(r["symbol"])],
                "trade_date": _as_date(r["trade_date"]),
                "source": source,
                "isin_observed": _as_str(r.get("isin")),
                "open": _as_float(r.get("open")),
                "high": _as_float(r.get("high")),
                "low": _as_float(r.get("low")),
                "close": _as_float(r.get("close")),
                "prev_close": _as_float(r.get("prev_close")),
                "volume": _as_float(r.get("volume")),
                "turnover": _as_float(r.get("turnover")),
                "trades": _as_float(r.get("trades")),
                "source_ts": _as_ts(r.get("source_ts")),
                "load_ts": now,
            }
        )
    return rows


def _load_corporate_actions(
    conn: Connection, frames: dict[str, pd.DataFrame], sec_ids: dict[str, int], now: datetime
) -> None:
    ca = frames["nse_corporate_actions"]
    prices = frames["nse_prices"]
    if ca.empty:
        return
    ca = ca[ca["symbol"].isin(list(sec_ids))]
    rows = [
        {
            "security_id": sec_ids[str(r["symbol"])],
            "ex_date": _as_date(r["ex_date"]),
            "record_date": _as_date(r.get("record_date")),
            "action_type": str(r["action_type"]),
            "subject": str(r["subject"]),
            "ratio": _as_float(r.get("ratio")),
            "amount": _as_float(r.get("amount")),
            "source_ts": _as_ts(r.get("source_ts")),
            "load_ts": now,
        }
        for r in records(ca)
        if _as_date(r.get("ex_date")) is not None
    ]
    upsert(conn, t.fact_corporate_action, rows, ["security_id", "ex_date", "subject"])

    events = event_factors(ca, prices) if not prices.empty else pd.DataFrame()
    if events.empty:
        return
    upsert(
        conn,
        t.fact_adjustment_factor,
        [
            {
                "security_id": sec_ids[str(r["symbol"])],
                "ex_date": _as_date(r["ex_date"]),
                "action_type": str(r["action_type"]),
                "factor": float(r["factor"]),
                "load_ts": now,
            }
            for r in records(events)
        ],
        ["security_id", "ex_date", "action_type"],
    )
    # Segments are derived wholesale from all known events: rebuild them for these securities.
    segments = build_segments(events)
    affected = sorted({sec_ids[str(s)] for s in segments["symbol"]})
    conn.execute(
        delete(t.adj_factor_segment).where(t.adj_factor_segment.c.security_id.in_(affected))
    )
    upsert(
        conn,
        t.adj_factor_segment,
        [
            {
                "security_id": sec_ids[str(r["symbol"])],
                "valid_from": r["valid_from"],
                "valid_to": r["valid_to"],
                "cumulative_factor": float(r["cumulative_factor"]),
            }
            for r in records(segments)
        ],
        ["security_id", "valid_from"],
    )


def _load_macro(conn: Connection, frames: dict[str, pd.DataFrame], now: datetime) -> None:
    series_ids = {
        (str(r[0]), str(r[1])): int(r[2])
        for r in conn.execute(
            select(
                t.dim_macro_series.c.source,
                t.dim_macro_series.c.code,
                t.dim_macro_series.c.series_id,
            )
        ).all()
    }
    for dataset, source in MACRO_SOURCES.items():
        df = frames[dataset]
        if df.empty:
            continue
        unknown = sorted(
            set(df["series_id"].astype(str)) - {c for s, c in series_ids if s == source}
        )
        if unknown:
            upsert(
                conn,
                t.dim_macro_series,
                [{"source": source, "code": c, "name": c} for c in unknown],
                ["source", "code"],
            )
            series_ids.update(
                {
                    (str(r[0]), str(r[1])): int(r[2])
                    for r in conn.execute(
                        select(
                            t.dim_macro_series.c.source,
                            t.dim_macro_series.c.code,
                            t.dim_macro_series.c.series_id,
                        )
                    ).all()
                }
            )
        rows = [
            {
                "series_id": series_ids[(source, str(r["series_id"]))],
                "obs_date": _as_date(r["obs_date"]),
                "vintage_date": _as_date(r.get("vintage_date")) or now.date(),
                "value": _as_float(r.get("value")),
                "source_ts": _as_ts(r.get("source_ts")),
                "load_ts": now,
            }
            for r in records(df)
        ]
        upsert(conn, t.fact_macro_series, rows, ["series_id", "obs_date", "vintage_date"])


def _load_fx(conn: Connection, fx: pd.DataFrame, now: datetime) -> None:
    if fx.empty:
        return
    ccy = _id_map(conn, t.dim_currency, "code", "currency_id")
    rows = [
        {
            "rate_date": _as_date(r["rate_date"]),
            "base_currency_id": ccy[str(r["base_ccy"])],
            "quote_currency_id": ccy[str(r["quote_ccy"])],
            "units": float(r["units"]),
            "rate": float(r["rate"]),
            "source": "fbil",
            "source_ts": _as_ts(r.get("source_ts")),
            "load_ts": now,
        }
        for r in records(fx)
        if str(r["base_ccy"]) in ccy and str(r["quote_ccy"]) in ccy
    ]
    upsert(
        conn,
        t.fact_fx_rate,
        rows,
        ["rate_date", "base_currency_id", "quote_currency_id", "source"],
    )


def load_anomalies(conn: Connection, anomalies: pd.DataFrame, now: datetime | None = None) -> int:
    """Upsert anomalies into dq_anomaly without touching `status` or `first_seen_ts`.

    A reviewer's decision ("reviewed", "false_positive") must survive every reload. The
    conflict update therefore only refreshes the descriptive columns and `last_seen_ts`.
    """
    if anomalies.empty:
        return 0
    now = now or _now()
    rows = [
        {
            "dataset": str(r["dataset"]),
            "record_key": str(r["record_key"]),
            "rule": str(r["rule"]),
            "severity": str(r["severity"]),
            "detail": str(r["detail"]),
            "detected_by": _as_str(r.get("detected_by")) or "rule",
            "symbol": _as_str(r.get("symbol")),
            "obs_date": _as_date(r.get("obs_date")),
            "value": _as_float(r.get("value")),
            "first_seen_ts": now,
            "last_seen_ts": now,
        }
        for r in records(anomalies)
    ]
    return upsert(
        conn,
        t.dq_anomaly,
        rows,
        ["dataset", "record_key", "rule"],
        update_cols=[
            "severity",
            "detail",
            "detected_by",
            "symbol",
            "obs_date",
            "value",
            "last_seen_ts",
        ],
    )


def load_frames(
    engine: Engine,
    frames: dict[str, pd.DataFrame],
    anomalies: pd.DataFrame | None = None,
    symbols: Iterable[str] | None = None,
) -> dict[str, int]:
    """Load canonical frames into an already-migrated database; return row counts per table."""
    frames = {name: frames.get(name, pd.DataFrame()) for name in ALL_DATASETS}
    universe = load_universe()
    sectors = {m.symbol: m.sector for m in universe.securities}
    syms = list(symbols) if symbols is not None else universe.symbols
    now = _now()
    with engine.begin() as conn:
        _seed_reference(conn, frames["nse_prices"])
        sec_ids = _load_securities(conn, frames, syms, sectors, now)
        sec_ids = {s: i for s, i in sec_ids.items() if s in set(syms)}
        upsert(
            conn,
            t.map_security_identifier,
            _identifier_rows(frames, sec_ids, now),
            ["security_id", "id_type", "id_value", "valid_from"],
        )
        price_key = ["security_id", "trade_date", "source"]
        upsert(
            conn,
            t.fact_price_daily,
            _price_rows(frames["nse_prices"], "nse", sec_ids, now),
            price_key,
        )
        upsert(
            conn,
            t.fact_price_daily,
            _price_rows(frames["yahoo_prices"], "yahoo", sec_ids, now),
            price_key,
        )
        _load_corporate_actions(conn, frames, sec_ids, now)
        _load_macro(conn, frames, now)
        _load_fx(conn, frames["fx_rates"], now)
        if anomalies is not None:
            load_anomalies(conn, anomalies, now)
        return {table.name: _count(conn, table) for table in t.metadata.sorted_tables}


def run_load(mode: str = "auto", settings: Settings | None = None) -> dict[str, int]:
    """Migrate the warehouse to head, then load every staged dataset and the anomalies.

    Args:
        mode: Input mode passed to validation if staging has to be (re)built.
        settings: Override settings (tests).

    Returns:
        Row count per warehouse table after the load.
    """
    s = settings or get_settings()
    upgrade(s.db_url)
    engine = get_engine(s.db_url)
    try:
        frames = load_staging_frames(s, mode)
        return load_frames(engine, frames, _read_anomalies(s))
    finally:
        engine.dispose()
