"""Warehouse schema: SQLAlchemy Core tables (the single definition used by Alembic and loaders).

Conventions (CLAUDE.md §6): surrogate integer keys on dimensions, natural keys enforced with
unique constraints, trading dates as DATE, `source_ts`/`load_ts` (UTC) on every fact, and prices
stored unadjusted with adjustment factors kept in their own tables.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = DateTime(timezone=True)

# --------------------------------------------------------------------------- dimensions

dim_exchange = Table(
    "dim_exchange",
    metadata,
    Column("exchange_id", Integer, primary_key=True, comment="Surrogate key"),
    Column(
        "mic", String(8), nullable=False, unique=True, comment="ISO 10383 market identifier code"
    ),
    Column("name", String(128), nullable=False, comment="Exchange name"),
    Column("country", String(2), nullable=False, comment="ISO 3166 alpha-2 country"),
    Column("timezone", String(64), nullable=False, comment="IANA timezone of the trading session"),
    Column("currency_code", String(3), nullable=False, comment="Trading currency"),
    comment="Exchanges / trading venues.",
)

dim_currency = Table(
    "dim_currency",
    metadata,
    Column("currency_id", Integer, primary_key=True, comment="Surrogate key"),
    Column("code", String(3), nullable=False, unique=True, comment="ISO 4217 code"),
    Column("name", String(64), nullable=False, comment="Currency name"),
    comment="Currencies (ISO 4217).",
)

dim_calendar = Table(
    "dim_calendar",
    metadata,
    Column("date_id", Integer, primary_key=True, comment="Surrogate key, yyyymmdd"),
    Column("cal_date", Date, nullable=False, unique=True, comment="Calendar date"),
    Column("year", Integer, nullable=False),
    Column("quarter", Integer, nullable=False),
    Column("month", Integer, nullable=False),
    Column("day_of_week", Integer, nullable=False, comment="0 = Monday"),
    Column("is_weekend", Boolean, nullable=False),
    Column("is_nse_trading_day", Boolean, nullable=False, comment="NSE cash market session held"),
    Column("holiday_name", String(128), nullable=True, comment="Reason when a weekday is closed"),
    comment="Calendar days with NSE trading-session flag.",
)

dim_issuer = Table(
    "dim_issuer",
    metadata,
    Column("issuer_id", Integer, primary_key=True, comment="Surrogate key"),
    Column(
        "lei", String(20), nullable=True, unique=True, comment="ISO 17442 Legal Entity Identifier"
    ),
    Column("legal_name", String(256), nullable=False, comment="Registered legal name (GLEIF)"),
    Column("country", String(2), nullable=True, comment="Legal address country"),
    Column("entity_status", String(16), nullable=True, comment="GLEIF entity status"),
    Column("parent_lei", String(20), nullable=True, comment="Direct parent LEI, if reported"),
    Column("load_ts", TS, nullable=False),
    comment="Legal entities that issue securities (GLEIF).",
)

dim_security = Table(
    "dim_security",
    metadata,
    Column("security_id", Integer, primary_key=True, comment="Surrogate key"),
    Column("exchange_id", ForeignKey("dim_exchange.exchange_id"), nullable=False),
    Column("symbol", String(32), nullable=False, comment="Exchange ticker"),
    Column("name", String(256), nullable=True, comment="Security / company name"),
    Column("sector", String(64), nullable=True, comment="NIFTY industry classification"),
    Column("security_type", String(32), nullable=True, comment="e.g. Common Stock (OpenFIGI)"),
    Column("issuer_id", ForeignKey("dim_issuer.issuer_id"), nullable=True),
    Column("face_value", Float, nullable=True, comment="Current face value, INR"),
    Column("listing_date", Date, nullable=True),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint("exchange_id", "symbol", name="uq_dim_security_listing"),
    comment="One row per exchange listing; identifiers live in map_security_identifier.",
)

dim_macro_series = Table(
    "dim_macro_series",
    metadata,
    Column("series_id", Integer, primary_key=True, comment="Surrogate key"),
    Column("source", String(16), nullable=False, comment="fred / worldbank"),
    Column("code", String(64), nullable=False, comment="Series code at the source"),
    Column("name", String(256), nullable=True),
    Column("frequency", String(16), nullable=True, comment="D / M / A"),
    Column("units", String(64), nullable=True),
    UniqueConstraint("source", "code", name="uq_dim_macro_series_source_code"),
    comment="Macro-economic time series catalogue.",
)

map_security_identifier = Table(
    "map_security_identifier",
    metadata,
    Column("map_id", Integer, primary_key=True),
    Column("security_id", ForeignKey("dim_security.security_id"), nullable=False),
    Column(
        "id_type", String(16), nullable=False, comment="ISIN / TICKER / FIGI / COMPOSITE_FIGI / LEI"
    ),
    Column("id_value", String(64), nullable=False),
    Column("source", String(16), nullable=False, comment="Where the mapping was observed"),
    Column("valid_from", Date, nullable=False, comment="First date the mapping is valid"),
    Column("valid_to", Date, nullable=True, comment="Last date valid; NULL = still current"),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint(
        "security_id", "id_type", "id_value", "valid_from", name="uq_map_security_identifier_nk"
    ),
    comment="Identifier history (SCD2): e.g. ISIN changes after a face-value split.",
)

# --------------------------------------------------------------------------- facts

fact_price_daily = Table(
    "fact_price_daily",
    metadata,
    Column("price_id", Integer, primary_key=True),
    Column("security_id", ForeignKey("dim_security.security_id"), nullable=False),
    Column("trade_date", Date, nullable=False),
    Column("source", String(16), nullable=False, comment="nse (primary) / yahoo (cross-check)"),
    Column("isin_observed", String(12), nullable=True, comment="ISIN printed on the source row"),
    Column("open", Float, nullable=True),
    Column("high", Float, nullable=True),
    Column("low", Float, nullable=True),
    Column("close", Float, nullable=True, comment="Unadjusted close (Yahoo: split-adjusted)"),
    Column("prev_close", Float, nullable=True),
    Column("volume", Float, nullable=True),
    Column("turnover", Float, nullable=True),
    Column("trades", Float, nullable=True),
    Column("source_ts", TS, nullable=True, comment="When the source published the record"),
    Column("load_ts", TS, nullable=False, comment="When we loaded it"),
    UniqueConstraint("security_id", "trade_date", "source", name="uq_fact_price_daily_nk"),
    comment="Daily OHLCV per listing and source, stored unadjusted.",
)

fact_corporate_action = Table(
    "fact_corporate_action",
    metadata,
    Column("ca_id", Integer, primary_key=True),
    Column("security_id", ForeignKey("dim_security.security_id"), nullable=False),
    Column("ex_date", Date, nullable=False),
    Column("record_date", Date, nullable=True),
    Column(
        "action_type",
        String(16),
        nullable=False,
        comment="dividend/split/bonus/rights/buyback/other",
    ),
    Column("subject", Text, nullable=False, comment="Source free text"),
    Column("ratio", Float, nullable=True, comment="Post/pre price ratio for split & bonus"),
    Column("amount", Float, nullable=True, comment="Cash per share, INR"),
    Column("source_ts", TS, nullable=True),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint("security_id", "ex_date", "subject", name="uq_fact_corporate_action_nk"),
    comment="Corporate actions with ex/record dates.",
)

fact_adjustment_factor = Table(
    "fact_adjustment_factor",
    metadata,
    Column("security_id", ForeignKey("dim_security.security_id"), primary_key=True),
    Column("ex_date", Date, primary_key=True),
    Column("action_type", String(16), primary_key=True),
    Column("factor", Float, nullable=False, comment="Multiply pre-ex-date prices by this"),
    Column("load_ts", TS, nullable=False),
    comment="Per-event price adjustment factors derived from corporate actions.",
)

adj_factor_segment = Table(
    "adj_factor_segment",
    metadata,
    Column("security_id", ForeignKey("dim_security.security_id"), primary_key=True),
    Column("valid_from", Date, primary_key=True),
    Column("valid_to", Date, nullable=False),
    Column(
        "cumulative_factor", Float, nullable=False, comment="Product of factors of later events"
    ),
    comment="Piecewise-constant cumulative factor; backs the v_price_adjusted view.",
)

fact_fundamental = Table(
    "fact_fundamental",
    metadata,
    Column("fundamental_id", Integer, primary_key=True),
    Column("security_id", ForeignKey("dim_security.security_id"), nullable=False),
    Column("period_end", Date, nullable=False),
    Column("metric", String(64), nullable=False),
    Column("value", Float, nullable=True),
    Column("currency_id", ForeignKey("dim_currency.currency_id"), nullable=True),
    Column("source", String(16), nullable=False),
    Column("source_ts", TS, nullable=True),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint(
        "security_id", "period_end", "metric", "source", name="uq_fact_fundamental_nk"
    ),
    comment="Quarterly fundamentals (not populated yet; see known issues).",
)

fact_macro_series = Table(
    "fact_macro_series",
    metadata,
    Column("macro_id", Integer, primary_key=True),
    Column("series_id", ForeignKey("dim_macro_series.series_id"), nullable=False),
    Column("obs_date", Date, nullable=False),
    Column("vintage_date", Date, nullable=False, comment="Date the value was observed"),
    Column("value", Float, nullable=True),
    Column("source_ts", TS, nullable=True),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint("series_id", "obs_date", "vintage_date", name="uq_fact_macro_series_nk"),
    comment="Macro observations; revisions are new vintages, never overwrites.",
)

fact_fx_rate = Table(
    "fact_fx_rate",
    metadata,
    Column("fx_id", Integer, primary_key=True),
    Column("rate_date", Date, nullable=False),
    Column("base_currency_id", ForeignKey("dim_currency.currency_id"), nullable=False),
    Column("quote_currency_id", ForeignKey("dim_currency.currency_id"), nullable=False),
    Column("units", Float, nullable=False, comment="Units of base currency quoted"),
    Column("rate", Float, nullable=False, comment="Quote currency per `units` base"),
    Column("source", String(16), nullable=False),
    Column("source_ts", TS, nullable=True),
    Column("load_ts", TS, nullable=False),
    UniqueConstraint(
        "rate_date", "base_currency_id", "quote_currency_id", "source", name="uq_fact_fx_rate_nk"
    ),
    comment="Reference FX fixings.",
)

# --------------------------------------------------------------------------- data quality

dq_metric = Table(
    "dq_metric",
    metadata,
    Column("metric_id", Integer, primary_key=True),
    Column("run_id", String(32), nullable=False, comment="Quality run identifier"),
    Column("run_ts", TS, nullable=False),
    Column("dataset", String(64), nullable=False),
    Column("dimension", String(16), nullable=False, comment="completeness/timeliness/accuracy/..."),
    Column("metric", String(64), nullable=False),
    Column("value", Float, nullable=True),
    Column("threshold", Float, nullable=True),
    Column("status", String(8), nullable=False, comment="pass / warn / fail / n/a"),
    UniqueConstraint("run_id", "dataset", "metric", name="uq_dq_metric_nk"),
    comment="Quality metric values per dataset per run.",
)

dq_anomaly = Table(
    "dq_anomaly",
    metadata,
    Column("anomaly_id", Integer, primary_key=True),
    Column("dataset", String(64), nullable=False),
    Column("record_key", String(128), nullable=False),
    Column("rule", String(64), nullable=False),
    Column("severity", String(8), nullable=False),
    Column("detail", Text, nullable=False),
    Column("detected_by", String(16), nullable=False, comment="rule / statistical / ml"),
    Column("symbol", String(64), nullable=True),
    Column("obs_date", Date, nullable=True),
    Column("value", Float, nullable=True),
    Column(
        "status",
        String(16),
        nullable=False,
        server_default="open",
        comment="open / reviewed / false_positive",
    ),
    Column("first_seen_ts", TS, nullable=False),
    Column("last_seen_ts", TS, nullable=False),
    Column("reviewed_ts", TS, nullable=True),
    UniqueConstraint("dataset", "record_key", "rule", name="uq_dq_anomaly_nk"),
    comment="Every flagged record; status survives reloads (upsert does not touch it).",
)
