"""Build a labelled sample of 50 anomaly flags and estimate detector precision.

Usage::

    uv run python scripts/eval_flags.py [--n 50] [--seed 7] [--out docs/quality-runs]

The script samples flags stratified by detector (rule / statistical / ml). For each one it
gathers checkable evidence from the warehouse and applies the explicit labelling criteria
documented in docs/quality-evaluation.md. It writes the labelling sheet (CSV) and prints
precision per detector.

The labels are produced mechanically by these criteria (written by the AI agent that built
the project). They are a first pass, and a human must re-check them; the CSV has an empty
`human_label` column for that.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from findata.model.db import get_engine
from findata.quality.metrics import load_warehouse, split_factors
from findata.settings import PROJECT_ROOT
from findata.validate.rules import cross_source_alignment

TOL_PCT = 0.5  # cross-source agreement tolerance, as in nse_prices.yaml
CONFIRM_PP = 1.0  # NSE and Yahoo returns within 1pp => Yahoo confirms the move
EXTREME_RET = 0.05  # |return| >= 5% is a market event worth a look on a NIFTY-50 name
EXTREME_VOL = 3.0  # volume >= 3x trailing median


def _evidence(flags: pd.DataFrame, wh: dict[str, pd.DataFrame]) -> pd.DataFrame:
    prices = wh["prices"]
    actions = wh["actions"]
    nse = prices[prices["source"] == "nse"].copy()
    nse["factor"] = split_factors(nse, actions).to_numpy()
    nse = nse.sort_values(["symbol", "trade_date"])
    nse["adj"] = nse["close"] * nse["factor"]
    nse["ret"] = nse.groupby("symbol")["adj"].pct_change()
    nse["vol_ratio"] = nse["volume"] / nse.groupby("symbol")["volume"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=5).median()
    )
    yh = prices[prices["source"] == "yahoo"]
    al = cross_source_alignment(nse, yh, actions)[
        ["symbol", "trade_date", "ret_2", "ret_diff_pct", "scale"]
    ]
    m = nse.merge(al, on=["symbol", "trade_date"], how="left").rename(
        columns={"ret_2": "y_ret", "ret_diff_pct": "xdiff_pct", "scale": "basis_ratio"}
    )
    m["ohlc_ok"] = (m["low"] <= m[["open", "close"]].min(axis=1)) & (
        m["high"] >= m[["open", "close"]].max(axis=1)
    )

    rows = []
    for f in flags.itertuples():
        ev: dict[str, object] = {"anomaly_id": f.anomaly_id}
        d = pd.Timestamp(f.obs_date) if pd.notna(f.obs_date) else None
        hit = (
            m[(m["symbol"] == f.symbol) & (m["trade_date"] == d)] if d is not None else m.iloc[0:0]
        )
        ca = actions[actions["symbol"] == f.symbol] if d is not None else actions.iloc[0:0]
        near = ca[(ca["ex_date"] - d).abs() <= pd.Timedelta(days=3)] if d is not None else ca
        ev["nearby_action"] = "; ".join(near["action_type"].astype(str)) if not near.empty else ""
        if hit.empty:
            ev.update(
                ret=np.nan,
                y_ret=np.nan,
                vol_ratio=np.nan,
                xdiff_pct=np.nan,
                basis_ratio=np.nan,
                ohlc_ok=None,
            )
        else:
            h = hit.iloc[0]
            ev.update(
                ret=h["ret"],
                y_ret=h["y_ret"],
                vol_ratio=h["vol_ratio"],
                xdiff_pct=h["xdiff_pct"],
                basis_ratio=h["basis_ratio"],
                ohlc_ok=bool(h["ohlc_ok"]),
            )
        rows.append(ev)
    return flags.merge(pd.DataFrame(rows), on="anomaly_id")


def label(r: pd.Series) -> tuple[str, str]:
    """Apply the documented criteria. Returns (label, reason)."""
    if r["rule"] == "cross_source_basis":
        return (
            "basis_mismatch",
            "informational: Yahoo restated history for a later corporate action",
        )
    if r["dataset"] != "nse_prices" or (pd.isna(r["ret"]) and pd.isna(r["xdiff_pct"])):
        if r["rule"] == "duplicate_keys":
            return "data_defect", "duplicate natural key is a defect by definition"
        if r["rule"] == "missing_trading_days":
            return "data_defect", "expected session absent from the source"
        return "unverifiable", "no price evidence available for this record"
    if pd.notna(r["obs_date"]) and pd.Timestamp(r["obs_date"]).weekday() >= 5:
        return "explained", "weekend special session (NSE DR drill); thin trading expected"
    if r["ohlc_ok"] is False:
        return "data_defect", "OHLC inconsistent on the source row"
    if pd.notna(r["xdiff_pct"]) and r["xdiff_pct"] > TOL_PCT:
        return "data_defect", f"NSE vs Yahoo daily returns differ by {r['xdiff_pct']:.2f}pp"
    if r["rule"] == "cross_source_basis":
        return (
            "basis_mismatch",
            "informational: Yahoo restated history for a later corporate action",
        )
    confirms = pd.notna(r["y_ret"]) and abs(r["ret"] - r["y_ret"]) * 100 <= CONFIRM_PP
    big = abs(r["ret"]) >= EXTREME_RET or (
        pd.notna(r["vol_ratio"]) and r["vol_ratio"] >= EXTREME_VOL
    )
    if r["nearby_action"] and not big:
        return "explained", f"corporate action nearby ({r['nearby_action']}), modest move"
    if big and confirms:
        return "market_event", "move confirmed by second source; genuine unusual session"
    if big:
        return "market_event", "large move/volume; second source unavailable to confirm"
    return "noise", "modest move and volume, sources agree"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "quality-runs")
    args = ap.parse_args()

    eng = get_engine()
    wh = load_warehouse(eng)
    flags = pd.read_sql("SELECT * FROM dq_anomaly", eng)
    flags["obs_date"] = pd.to_datetime(flags["obs_date"])
    if flags.empty:
        raise SystemExit("no flags in dq_anomaly: run `findata load` and `findata quality run`")
    groups = flags.groupby("detected_by")
    per = max(1, args.n // groups.ngroups)
    sample = pd.concat(
        [g.sample(min(len(g), per), random_state=args.seed) for _, g in groups]
    ).head(args.n)
    if len(sample) < args.n:  # top up from whatever is left
        rest = flags.drop(sample.index)
        sample = pd.concat(
            [sample, rest.sample(min(len(rest), args.n - len(sample)), random_state=args.seed)]
        )
    sheet = _evidence(sample.reset_index(drop=True), wh)
    labelled = sheet.apply(label, axis=1, result_type="expand")
    sheet["agent_label"], sheet["agent_reason"] = labelled[0], labelled[1]
    sheet["human_label"] = ""
    useful = sheet["agent_label"].isin(["data_defect", "market_event"])
    sheet["useful"] = useful
    sheet["data_defect"] = sheet["agent_label"] == "data_defect"

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "eval_flags_sheet.csv"
    cols = [
        "anomaly_id", "dataset", "rule", "detected_by", "severity", "symbol", "obs_date",
        "ret", "y_ret", "vol_ratio", "xdiff_pct", "basis_ratio", "ohlc_ok", "nearby_action",
        "agent_label", "agent_reason", "human_label", "detail",
    ]  # fmt: skip
    sheet[cols].to_csv(path, index=False)
    summary = sheet.groupby("detected_by").agg(
        flags=("anomaly_id", "count"),
        precision_useful=("useful", "mean"),
        precision_data_defect=("data_defect", "mean"),
    )
    summary.loc["ALL"] = [len(sheet), useful.mean(), sheet["data_defect"].mean()]
    print(f"sheet: {path}")
    print(sheet["agent_label"].value_counts().to_string())
    print(summary.to_string(float_format=lambda x: f"{x:.2f}"))


if __name__ == "__main__":
    main()
