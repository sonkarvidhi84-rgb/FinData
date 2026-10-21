"""FinData Quality dashboard: Quality Overview, Anomalies and Analytics.

Run with `uv run streamlit run dashboard/app.py`. Works offline. If the warehouse is empty it
builds it from `data/samples` (via `findata load`) and runs one quality pass first.
All data logic lives in `findata.quality.views` / `findata.analytics.queries`; this file only
lays out the page.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import Engine

from findata.quality import views

st.set_page_config(page_title="FinData Quality", layout="wide")

STATUS_LABEL = {"pass": "🟢 pass", "warn": "🟡 warn", "fail": "🔴 fail", "n/a": "⚪ n/a"}

METHODOLOGY = """
**Completeness.** Of the rows we *should* have (each security × each NSE trading session from
its first appearance, using the exchange calendar), what share is present? For macro and
reference data: the share of required fields that are filled in.

**Timeliness.** How many hours after the scheduled event (15:30 IST market close for
bhavcopy, 13:30 IST fixing for FBIL FX) the source published the data. This measures the
source's own latency (HTTP `Last-Modified`), not how late our backfill ran.

**Accuracy.** The share of trading days on which NSE's daily return agrees with Yahoo
Finance's to within 0.5 percentage points. NSE prices are split-adjusted first. Returns are
compared rather than price levels because Yahoo restates its whole history for later bonuses,
splits and demergers: its 2024 prices for HDFCBANK are half of NSE's, which is a basis
difference, not an error. The number of symbols on a different basis is reported separately.

**Consistency.** Internal logic, so that low ≤ open/close ≤ high, and cross-source identifier
agreement: does the instrument OpenFIGI returns for each ISIN carry the same company name as
NSE's master? (Tickers can't be compared, because OpenFIGI uses Bloomberg tickers such as
INFO for Infosys.)

**Validity.** The share of rows that pass the pandera schema generated from the dataset's YAML
contract (types, ranges, ISIN format, allowed values).

**Uniqueness.** The share of rows whose natural key (e.g. ISIN + trade date) is not duplicated.

**Anomaly layers.** (1) Hard rules such as OHLC sanity, stale prices or jumps without a
corporate action. (2) Rolling z-scores, where each stock is judged against its own last 20
sessions. (3) An IsolationForest over return, volume ratio, intraday spread and cross-source
difference, which catches unusual *combinations*. Every flag carries a severity and a
plain-language explanation. An analyst can mark it reviewed or as a false positive, and that
decision survives reloads.
"""


@st.cache_resource
def get_engine() -> Engine:
    from findata.quality.runner import get_warehouse_engine

    return get_warehouse_engine()


def ensure_warehouse(engine: Engine) -> None:
    """Build the warehouse from samples on first launch and run one quality pass."""
    if not views.warehouse_is_empty(engine):
        if views.metric_history(engine).empty:
            with st.spinner("Running first quality pass..."):
                from findata.quality.runner import run_quality

                run_quality(engine)
        return
    with st.spinner("Building warehouse from data/samples (first run only)..."):
        from findata.model.loaders import run_load
        from findata.quality.runner import run_quality

        run_load("auto")
        run_quality(engine)


def quality_overview(engine: Engine) -> None:
    latest = views.latest_metrics(engine)
    if latest.empty:
        st.info("No quality runs yet. Run `findata quality run`.")
        return
    last_ts = pd.Timestamp(latest["run_ts"].max())
    counts = latest["status"].value_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last run (UTC)", f"{last_ts:%Y-%m-%d %H:%M}")
    c2.metric("Pass", int(counts.get("pass", 0)))
    c3.metric("Warn", int(counts.get("warn", 0)))
    c4.metric("Fail", int(counts.get("fail", 0)))

    table = latest[["dataset", "dimension", "metric", "value", "threshold", "status"]].copy()
    table["status"] = table["status"].map(STATUS_LABEL).fillna(table["status"])
    st.subheader("Latest run")
    st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Metric history")
    hist = views.metric_history(engine)
    dataset = st.selectbox("Dataset", sorted(hist["dataset"].unique()), key="hist_ds")
    h = hist[(hist["dataset"] == dataset) & hist["value"].notna()]
    if h.empty:
        st.caption("No numeric values for this dataset.")
    else:
        fig = px.line(h, x="run_ts", y="value", color="metric", markers=True)
        fig.update_layout(xaxis_title="run", yaxis_title="value", legend_title="metric")
        st.plotly_chart(fig, width="stretch")

    with st.expander("Methodology"):
        st.markdown(METHODOLOGY)


def _filter(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    options = sorted(df[col].dropna().astype(str).unique())
    chosen = st.multiselect(label, options, key=f"f_{col}")
    return df[df[col].astype(str).isin(chosen)] if chosen else df


def price_chart(engine: Engine, row: pd.Series) -> go.Figure | None:
    """NSE vs Yahoo close for the anomaly's symbol, flagged date and ex-dates marked."""
    symbol = str(row["symbol"]) if pd.notna(row["symbol"]) else ""
    prices = views.price_series(engine, symbol) if symbol else pd.DataFrame()
    if prices.empty:
        return None
    fig = go.Figure()
    for source, dash in (("nse", "solid"), ("yahoo", "dot")):
        p = prices[prices["source"] == source]
        if not p.empty:
            fig.add_trace(
                go.Scatter(
                    x=p["trade_date"],
                    y=p["close"],
                    name=f"{source} close",
                    mode="lines",
                    line={"dash": dash},
                )
            )
    if pd.notna(row["obs_date"]):
        hit = prices[(prices["source"] == "nse") & (prices["trade_date"] == row["obs_date"])]
        if not hit.empty:
            fig.add_trace(
                go.Scatter(
                    x=hit["trade_date"],
                    y=hit["close"],
                    name="flagged",
                    mode="markers",
                    marker={"size": 14, "symbol": "x"},
                )
            )
    actions = views.corporate_actions(engine, symbol)
    for a in actions.itertuples():
        fig.add_vline(x=a.ex_date, line_dash="dash", line_width=1, opacity=0.5)
    if not actions.empty:
        lo = float(prices["close"].min())
        fig.add_trace(
            go.Scatter(
                x=actions["ex_date"],
                y=[lo] * len(actions),
                mode="markers",
                name="corporate action (ex-date)",
                text=actions["subject"],
                hovertemplate="%{x|%Y-%m-%d}<br>%{text}<extra></extra>",
                marker={"symbol": "triangle-up", "size": 10},
            )
        )
    fig.update_layout(
        title=f"{symbol}: unadjusted NSE close vs Yahoo (split-adjusted)",
        xaxis_title=None,
        yaxis_title="INR",
        hovermode="x unified",
    )
    return fig


def anomalies_tab(engine: Engine) -> None:
    df = views.anomalies(engine)
    if df.empty:
        st.info("No anomalies recorded.")
        return
    cols = st.columns(5)
    filtered = df
    for col, (field, label) in zip(
        cols,
        [
            ("dataset", "Dataset"),
            ("rule", "Rule"),
            ("severity", "Severity"),
            ("status", "Status"),
            ("detected_by", "Detected by"),
        ],
        strict=True,
    ):
        with col:
            filtered = _filter(filtered, field, label)
    st.caption(f"{len(filtered)} of {len(df)} anomalies")
    show = ["anomaly_id", "dataset", "rule", "severity", "detected_by", "symbol", "obs_date"]
    st.dataframe(
        filtered[[*show, "status", "detail"]].sort_values(["obs_date", "symbol"]),
        hide_index=True,
        width="stretch",
    )
    if filtered.empty:
        return
    labels = {
        int(r.anomaly_id): f"#{r.anomaly_id} {r.symbol or ''} {r.rule} "
        f"{'' if pd.isna(r.obs_date) else f'{r.obs_date:%Y-%m-%d}'}"
        for r in filtered.itertuples()
    }
    chosen = st.selectbox(
        "Inspect anomaly", list(labels), format_func=lambda i: labels[i], key="inspect"
    )
    row = filtered.set_index("anomaly_id").loc[chosen].copy()
    row["anomaly_id"] = chosen
    st.markdown(f"**{row['rule']}** ({row['severity']}, {row['detected_by']}): {row['detail']}")
    fig = price_chart(engine, row)
    if fig is not None:
        st.plotly_chart(fig, width="stretch")
    b1, b2, b3, _ = st.columns([1, 1, 1, 3])
    actions: list[tuple[Any, str, str]] = [
        (b1, "Mark as reviewed", "reviewed"),
        (b2, "False positive", "false_positive"),
        (b3, "Reopen", "open"),
    ]
    for col, label, status in actions:
        if col.button(label, key=f"btn_{status}"):
            views.set_anomaly_status(engine, int(chosen), status)
            st.toast(f"Anomaly #{chosen} -> {status}")
            st.rerun()


def _analytics_section(
    title: str, fn_name: str, render: Callable[[pd.DataFrame], go.Figure], engine: Engine
) -> None:
    st.subheader(title)
    try:
        from findata.analytics import queries

        df = getattr(queries, fn_name)(engine)
    except Exception as exc:  # the tab must never take the dashboard down
        st.warning(f"{fn_name} unavailable: {exc}")
        return
    if df is None or df.empty:
        st.info("Not enough data for this query.")
        return
    st.plotly_chart(render(df), width="stretch")
    with st.expander("Data"):
        st.dataframe(df, hide_index=True, width="stretch")


def analytics_tab(engine: Engine) -> None:
    def dividends(df: pd.DataFrame) -> go.Figure:
        fig = px.scatter(
            df,
            x="ex_date",
            y="excess_return",
            color="sector",
            hover_data=["symbol", "amount", "stock_return", "sector_return"],
        )
        fig.add_hline(y=0, line_width=1, opacity=0.5)
        fig.update_layout(yaxis_tickformat=".1%", yaxis_title="sector-adjusted return")
        return fig

    def hedged(df: pd.DataFrame) -> go.Figure:
        long = df.melt(
            id_vars=["symbol"],
            value_vars=["usd_unhedged_return", "usd_hedged_return"],
            var_name="investor",
            value_name="return",
        )
        fig = px.bar(long, x="symbol", y="return", color="investor", barmode="group")
        fig.update_layout(yaxis_tickformat=".1%", xaxis_title=None)
        return fig

    def corr(df: pd.DataFrame) -> go.Figure:
        d = df.sort_values("correlation")
        fig = px.bar(d, x="correlation", y="symbol", orientation="h", color="sector")
        fig.update_layout(yaxis_title=None, xaxis_title="corr(daily return, Δ US 10y yield)")
        return fig

    _analytics_section(
        "Sector-adjusted 5-day returns around dividend ex-dates",
        "dividend_event_returns",
        dividends,
        engine,
    )
    _analytics_section(
        "USD investor: INR-hedged vs unhedged returns (FBIL USD/INR)",
        "inr_hedged_vs_unhedged",
        hedged,
        engine,
    )
    _analytics_section(
        "NIFTY constituents vs US 10y Treasury yield changes (FRED DGS10, lagged 1 day)",
        "nifty_vs_us10y_correlation",
        corr,
        engine,
    )


def main() -> None:
    st.title("FinData Quality Pipeline")
    engine = get_engine()
    ensure_warehouse(engine)
    tab1, tab2, tab3 = st.tabs(["Quality Overview", "Anomalies", "Analytics"])
    with tab1:
        quality_overview(engine)
    with tab2:
        anomalies_tab(engine)
    with tab3:
        analytics_tab(engine)


main()
