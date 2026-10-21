"""Statistical and ML anomaly detection on daily prices (layers 2 and 3 of the framework).

Layer 1 (hard rules) lives in `findata.validate.rules`. This module adds:

* **Rolling z-scores.** A return or volume far outside a stock's own recent behaviour. Each
  stock is judged against itself, because a 5% day is normal for a metals stock and rare for
  an FMCG stock.
* **IsolationForest.** A multivariate detector on return, volume ratio, intraday spread and
  cross-source difference. It catches combinations that no single threshold flags, such as a
  modest move on 8x volume with a wide spread.

All returns and volumes are split-adjusted first. Otherwise every split day would look like a
-90% crash, and the detectors would only rediscover the corporate-action calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from findata.quality.metrics import split_factors
from findata.validate.anomaly import Anomaly, Severity
from findata.validate.rules import cross_source_alignment

FEATURES = ["ret", "log_volume_ratio", "spread", "xdiff"]
CONTAMINATION_GRID = [0.002, 0.005, 0.01, 0.02]
DEFAULT_CONTAMINATION = 0.01
WEAK_LABEL_RULES = ("unexplained_jump", "cross_source_price_diff", "ohlc_sanity")
FEATURE_LABELS = {
    "ret": "daily log return",
    "log_volume_ratio": "log(volume / 20-session median)",
    "spread": "(high - low) / close",
    "xdiff": "|NSE - Yahoo| daily return difference",
}


def _key(symbol: Any, d: Any) -> str:
    return f"{symbol}|{pd.Timestamp(d):%Y-%m-%d}"


def price_features(
    prices: pd.DataFrame,
    secondary: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    window: int = 20,
) -> pd.DataFrame:
    """Engineer per-(symbol, trade_date) features on a split-adjusted basis.

    Args:
        prices: Primary prices with `symbol, trade_date, open, high, low, close, volume`.
        secondary: Second-source closes (`symbol, trade_date, close`) for `xdiff`.
        actions: Corporate actions (`symbol, ex_date, action_type, ratio`).
        window: Look-back for the volume median.

    Returns:
        Frame with the identifying columns plus `adj_close`, `volume_adj` and FEATURES.
        The first row of each symbol has NaN `ret`.
    """
    cols = ["symbol", "trade_date", "open", "high", "low", "close", "volume"]
    p = prices[cols].copy()
    p["trade_date"] = pd.to_datetime(p["trade_date"]).dt.normalize()
    p = p.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    factor = split_factors(p, actions)
    p["adj_close"] = p["close"] * factor
    p["volume_adj"] = p["volume"] / factor
    g = p.groupby("symbol", sort=False)
    p["ret"] = np.log(p["adj_close"] / g["adj_close"].shift(1))
    med = g["volume_adj"].transform(
        lambda s: s.shift(1).rolling(window, min_periods=max(3, window // 4)).median()
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        p["log_volume_ratio"] = np.log(p["volume_adj"] / med)
    p["log_volume_ratio"] = p["log_volume_ratio"].replace([np.inf, -np.inf], np.nan)
    p["spread"] = (p["high"] - p["low"]) / p["close"]
    p["xdiff"] = np.nan
    if secondary is not None and not secondary.empty:
        # Compare returns, not levels: Yahoo restates history for corporate actions after
        # our window, which shifts its price level by a constant but leaves returns intact.
        aligned = cross_source_alignment(p, secondary, actions)
        aligned["xdiff"] = aligned["ret_diff_pct"] / 100.0
        p = p.drop(columns="xdiff").merge(
            aligned[["symbol", "trade_date", "xdiff"]], on=["symbol", "trade_date"], how="left"
        )
    return p


def _trailing_z(x: pd.Series, groups: pd.Series, window: int) -> pd.Series:
    """z-score of each value against the trailing `window` values of the same group (excl. self)."""

    def roll(s: pd.Series, how: str) -> pd.Series:
        r = s.shift(1).rolling(window, min_periods=max(5, window // 2))
        return r.mean() if how == "mean" else r.std()

    mu = x.groupby(groups, sort=False).transform(lambda s: roll(s, "mean"))
    sd = x.groupby(groups, sort=False).transform(lambda s: roll(s, "std"))
    z = (x - mu) / sd.where(sd > 0)
    return z


def rolling_zscore_flags(
    features: pd.DataFrame,
    window: int = 20,
    threshold: float = 4.0,
    dataset: str = "nse_prices",
) -> list[Anomaly]:
    """Flag returns and volumes more than `threshold` sigmas from the stock's trailing window.

    Financial problem caught: a print far out of line with the stock's own recent behaviour.
    It may be a bad tick, a missing corporate action or a real event that an analyst should
    know about. Returns use split-adjusted closes. Volume is tested on its log, because
    volume is heavy-tailed.
    """
    out: list[Anomaly] = []
    specs = [
        ("zscore_return", "ret", "log return"),
        ("zscore_volume", "log_volume_ratio", "log volume ratio"),
    ]
    weekend = pd.to_datetime(features["trade_date"]).dt.weekday >= 5
    for rule, col, label in specs:
        x = features[col].astype("float64")
        if col == "log_volume_ratio":
            # NSE's occasional Saturday sessions (DR-site drills, special sessions) trade a
            # few percent of normal volume by design. They are neither flagged nor allowed
            # into the trailing window, where they would distort the next 20 sessions' stats.
            x = x.where(~weekend)
        z = _trailing_z(x, features["symbol"], window)
        hits = features.assign(_z=z)[z.abs() > threshold]
        for _, r in hits.iterrows():
            zval = float(r["_z"])
            sev: Severity = "high" if abs(zval) > 2 * threshold else "medium"
            day = pd.Timestamp(r["trade_date"])
            note = " (weekend special session)" if day.weekday() >= 5 else ""
            if col == "ret":
                what = f"return {np.expm1(r['ret']):+.2%}"
            else:
                what = f"volume {np.exp(r['log_volume_ratio']):.1f}x its trailing median"
            out.append(
                Anomaly(
                    dataset=dataset,
                    rule=rule,
                    severity=sev,
                    record_key=_key(r["symbol"], r["trade_date"]),
                    detail=(
                        f"{r['symbol']} {pd.Timestamp(r['trade_date']):%Y-%m-%d}: {what} is "
                        f"{zval:+.1f} sigma from its trailing {window}-session {label} mean.{note}"
                    ),
                    symbol=str(r["symbol"]),
                    obs_date=f"{pd.Timestamp(r['trade_date']):%Y-%m-%d}",
                    value=zval,
                    detected_by="statistical",
                )
            )
    return out


def feature_matrix(features: pd.DataFrame) -> np.ndarray:
    """Model input: FEATURES with missing values set to 0 (= 'nothing unusual')."""
    return features[FEATURES].astype("float64").fillna(0.0).to_numpy()


def weak_labels(features: pd.DataFrame, rule_anomalies: pd.DataFrame) -> np.ndarray:
    """Boolean label per feature row: was the (symbol, date) flagged by a hard rule?

    Hard-rule flags are imperfect ("weak") labels, but they are the only labelled signal
    available without a human labeller, so they are used to choose the contamination rate.
    """
    if rule_anomalies.empty:
        return np.zeros(len(features), dtype=bool)
    a = rule_anomalies[rule_anomalies["rule"].isin(WEAK_LABEL_RULES)]
    a = a[a["symbol"].notna() & a["obs_date"].notna()]
    keys = {_key(s, d) for s, d in zip(a["symbol"], a["obs_date"], strict=True)}
    return np.array(
        [
            _key(s, d) in keys
            for s, d in zip(features["symbol"], features["trade_date"], strict=True)
        ],
        dtype=bool,
    )


def _f1(pred: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    tp = float((pred & y).sum())
    precision = tp / pred.sum() if pred.sum() else 0.0
    recall = tp / y.sum() if y.sum() else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def calibrate_contamination(
    X: np.ndarray,
    y: np.ndarray,
    grid: list[float] = CONTAMINATION_GRID,
    random_state: int = 42,
) -> tuple[float, pd.DataFrame]:
    """Pick the contamination that maximises F1 against the weak labels.

    Ties go to the smaller value, because fewer flags means less analyst time. With no
    positive labels at all, F1 is undefined and DEFAULT_CONTAMINATION is returned.

    Returns:
        (chosen contamination, table of contamination/flagged/precision/recall/f1).
    """
    rows = []
    for c in grid:
        model = IsolationForest(contamination=c, random_state=random_state, n_estimators=200)
        pred = model.fit_predict(X) == -1
        precision, recall, f1 = _f1(pred, y)
        rows.append(
            {
                "contamination": c,
                "flagged": int(pred.sum()),
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    table = pd.DataFrame(rows)
    if not y.any():
        return DEFAULT_CONTAMINATION, table
    best = table.sort_values(["f1", "contamination"], ascending=[False, True]).iloc[0]
    return float(best["contamination"]), table


@dataclass
class MLResult:
    """Output of the IsolationForest layer."""

    anomalies: list[Anomaly]
    contamination: float
    calibration: pd.DataFrame
    model_path: Path | None


def isolation_forest_flags(
    features: pd.DataFrame,
    rule_anomalies: pd.DataFrame,
    model_dir: Path | None = None,
    dataset: str = "nse_prices",
    random_state: int = 42,
) -> MLResult:
    """Fit a calibrated IsolationForest and return its flags with explanations.

    The explanation names the feature with the largest robust z-score (distance from the
    median in MAD units). The forest itself is not additive, so this is a heuristic. It is
    still the question an analyst asks first: what is weird about this row?
    """
    usable = features[features["ret"].notna()].reset_index(drop=True)
    if len(usable) < 50:
        return MLResult([], DEFAULT_CONTAMINATION, pd.DataFrame(), None)
    X = feature_matrix(usable)
    y = weak_labels(usable, rule_anomalies)
    contamination, table = calibrate_contamination(X, y, random_state=random_state)
    model = IsolationForest(
        contamination=contamination, random_state=random_state, n_estimators=200
    ).fit(X)
    pred = model.predict(X) == -1
    scores = -model.score_samples(X)

    med = np.median(X, axis=0)
    mad = np.median(np.abs(X - med), axis=0) * 1.4826
    mad[mad == 0] = np.nan
    robust_z = np.abs((X - med) / mad)

    model_path: Path | None = None
    if model_dir is not None:
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "isolation_forest.joblib"
        joblib.dump(
            {"model": model, "features": FEATURES, "contamination": contamination}, model_path
        )

    out: list[Anomaly] = []
    for i in np.flatnonzero(pred):
        r = usable.iloc[i]
        rz = np.nan_to_num(robust_z[i], nan=0.0)
        top = int(np.argmax(rz))
        fname = FEATURES[top]
        sev: Severity = "high" if rz[top] >= 8 else "medium"
        vals = ", ".join(f"{f}={X[i, j]:+.4f}" for j, f in enumerate(FEATURES))
        out.append(
            Anomaly(
                dataset=dataset,
                rule="isolation_forest",
                severity=sev,
                record_key=_key(r["symbol"], r["trade_date"]),
                detail=(
                    f"{r['symbol']} {pd.Timestamp(r['trade_date']):%Y-%m-%d}: unusual "
                    f"combination (anomaly score {scores[i]:.3f}, contamination "
                    f"{contamination}). Most unusual feature: {FEATURE_LABELS[fname]} "
                    f"({rz[top]:.1f} robust sigma). Features: {vals}."
                ),
                symbol=str(r["symbol"]),
                obs_date=f"{pd.Timestamp(r['trade_date']):%Y-%m-%d}",
                value=float(scores[i]),
                detected_by="ml",
            )
        )
    return MLResult(out, contamination, table, model_path)
