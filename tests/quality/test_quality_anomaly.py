from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from findata.quality import anomaly as qa

DATES = pd.bdate_range("2024-01-01", periods=200)


def _series(n: int = 80, spike_at: int | None = None, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, n)
    vol = rng.integers(900_000, 1_100_000, n).astype(float)
    if spike_at is not None:
        rets[spike_at] = 0.2
        vol[spike_at] = 10_000_000
    close = 100 * np.exp(np.cumsum(rets))
    return pd.DataFrame(
        {
            "symbol": "X",
            "trade_date": DATES[:n],
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": vol,
        }
    )


def test_zscore_flags_injected_spike() -> None:
    flags = qa.rolling_zscore_flags(qa.price_features(_series(spike_at=50)))
    day = f"{DATES[50]:%Y-%m-%d}"
    rules = {(a.rule, a.obs_date) for a in flags}
    assert ("zscore_return", day) in rules
    assert ("zscore_volume", day) in rules
    a = next(a for a in flags if a.rule == "zscore_return")
    assert a.detected_by == "statistical"
    assert a.record_key == f"X|{day}"
    assert "sigma" in a.detail


def test_weekend_special_session_volume_not_flagged() -> None:
    df = _series(n=60)
    sat = pd.Timestamp("2024-03-02")  # a real NSE Saturday DR-drill session
    df.loc[40, "trade_date"] = sat
    df = df.sort_values("trade_date").reset_index(drop=True)
    df.loc[df["trade_date"] == sat, "volume"] = 20_000.0  # ~2% of normal
    flags = qa.rolling_zscore_flags(qa.price_features(df))
    assert not [a for a in flags if a.rule == "zscore_volume"]


def test_zscore_quiet_on_clean_series() -> None:
    assert qa.rolling_zscore_flags(qa.price_features(_series())) == []


def test_split_is_not_a_return_anomaly() -> None:
    df = _series()
    ex = df["trade_date"].iloc[40]
    pre = df["trade_date"] < ex
    for c in ("open", "high", "low", "close"):
        df.loc[pre, c] *= 10
    df.loc[pre, "volume"] /= 10
    acts = pd.DataFrame(
        {"symbol": ["X"], "ex_date": [ex], "action_type": ["split"], "ratio": [0.1]}
    )
    assert qa.rolling_zscore_flags(qa.price_features(df, actions=acts)) == []
    # without the corporate action the split looks like a crash
    unadjusted = qa.rolling_zscore_flags(qa.price_features(df))
    assert any(a.rule == "zscore_return" for a in unadjusted)


def test_price_features_cross_source_diff_uses_returns() -> None:
    p = _series(n=10)
    # Yahoo on a constant different basis (restated history) plus one bad print on day 4
    y_close = p["close"] / 2.0
    y_close.iloc[4] = y_close.iloc[4] * 1.05
    f = qa.price_features(p, secondary=p.assign(close=y_close))
    clean = f["xdiff"].drop(index=[0, 4, 5])
    assert clean.abs().max() < 1e-9
    assert f["xdiff"].iloc[4] > 0.04  # into the bad print
    assert f["xdiff"].iloc[5] > 0.04  # and back out of it
    assert np.isnan(f["ret"].iloc[0])


def test_weak_labels() -> None:
    f = qa.price_features(_series(n=5))
    rule = pd.DataFrame(
        {
            "rule": ["unexplained_jump", "duplicate_keys"],
            "symbol": ["X", "X"],
            "obs_date": [f["trade_date"].iloc[1], f["trade_date"].iloc[2]],
        }
    )
    assert qa.weak_labels(f, rule).tolist() == [False, True, False, False, False]
    assert not qa.weak_labels(f, pd.DataFrame()).any()


def test_calibrate_contamination_returns_grid_value() -> None:
    rng = np.random.default_rng(1)
    X = rng.normal(0, 1, (500, 4))
    X[:5] += 12
    y = np.zeros(500, dtype=bool)
    y[:5] = True
    c, table = qa.calibrate_contamination(X, y)
    assert c in qa.CONTAMINATION_GRID
    assert c == 0.01  # 5/500 outliers -> 1% maximises F1
    assert set(table.columns) == {"contamination", "flagged", "precision", "recall", "f1"}
    c0, _ = qa.calibrate_contamination(X, np.zeros(500, dtype=bool))
    assert c0 == qa.DEFAULT_CONTAMINATION


def test_isolation_forest_flags_and_persists(tmp_path: Path) -> None:
    frames = [
        _series(n=120, seed=s, spike_at=90 if s == 0 else None).assign(symbol=f"S{s}")
        for s in range(4)
    ]
    feats = qa.price_features(pd.concat(frames, ignore_index=True))
    labels = pd.DataFrame({"rule": ["unexplained_jump"], "symbol": ["S0"], "obs_date": [DATES[90]]})
    res = qa.isolation_forest_flags(feats, labels, model_dir=tmp_path)
    assert res.contamination in qa.CONTAMINATION_GRID
    assert res.model_path is not None and res.model_path.exists()
    assert res.anomalies and all(a.detected_by == "ml" for a in res.anomalies)
    assert any(a.symbol == "S0" and a.obs_date == f"{DATES[90]:%Y-%m-%d}" for a in res.anomalies)
    assert "Most unusual feature" in res.anomalies[0].detail


def test_isolation_forest_needs_enough_rows() -> None:
    res = qa.isolation_forest_flags(qa.price_features(_series(n=20)), pd.DataFrame())
    assert res.anomalies == []
    assert res.model_path is None
