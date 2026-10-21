# ADR 0004: Three anomaly-detection layers, with the IsolationForest calibrated on rule flags

**Status:** accepted

**Context.** "Is this price wrong?" has no single test. Some errors break hard logic, such as
low > high or a duplicate key. Some are only implausible *for this stock*: a 6% move is routine
for TATASTEEL and extraordinary for HINDUNILVR. Some are only visible as a combination, for
example a modest move on 8x normal volume with an unusually wide range.

**Decision.** Detection runs as three layers that write to the same `dq_anomaly` table, each
tagged with `detected_by`.

1. **Hard rules** (`validate/rules.py`). These are deterministic and explainable, and have
   zero false positives on the logic they encode. They run before load.
2. **Rolling z-scores** (`quality/anomaly.py`). Each stock is judged against its own trailing
   20 sessions (mean and standard deviation exclude the day itself) on log returns and log
   volume ratio. A flag needs |z| > 4, and severity is high above 8.
3. **IsolationForest** on four features: return, log(volume / 20-day median), (high-low)/close
   and |NSE - Yahoo| daily-return difference. Missing features are set to 0, meaning "nothing unusual".

Every series is **split-adjusted first**, using the corporate-actions table. Otherwise each
split would be a -90% "crash" and layers 2 and 3 would just rediscover the corporate-action
calendar.

**Calibrating `contamination`.** IsolationForest needs the expected outlier share up front.
There are no human labels, so we use the hard-rule flags (`unexplained_jump`,
`cross_source_price_diff`, `ohlc_sanity`) as *weak labels*. We fit the forest for each value
in {0.2%, 0.5%, 1%, 2%} and keep the one with the best F1 against those labels. Ties go to
the smaller rate, because analyst time is the scarce resource. With no positive labels the
default is 1%. The calibration table is written into every run report under
`docs/quality-runs/`.

**Outcome on the 2024-H1 samples.** The only weak positive was `unexplained_jump` (NTPC on
2024-06-04), so the grid search picked the smallest rate, **0.2%** (5 flags, F1 0.33 against
one label). All 5 flags fell on election-results day and are confirmed by Yahoo. With so few
weak labels the calibration is close to a "smallest sensible rate" default. Record it as
such, and recalibrate once human labels exist (`docs/quality-evaluation.md`).

The cross-source feature uses the absolute *daily-return* difference from
`validate.rules.cross_source_alignment`, not price levels. Yahoo restates its history for
later corporate actions (0.5x for HDFCBANK/RELIANCE/NESTLEIND, 0.2x for KOTAKBANK), which
would otherwise make every row of those stocks look anomalous. NSE Saturday special sessions
are excluded from the volume z-score (see the quality evaluation).

**Trade-offs.**
- The weak labels are biased toward what the rules already catch, so this calibration slightly
  rewards the forest for agreeing with the rules. The forest's value is the flags the rules
  *don't* raise, and its precision on those is measured separately in
  `quality-evaluation.md`.
- The "top contributing feature" in each explanation is the feature with the largest robust
  z-score (MAD units). That is a heuristic, not a SHAP attribution. It is cheap and readable,
  and it answers the analyst's first question.
- IsolationForest is unsupervised and is refit each run (`random_state=42` makes it
  reproducible). That suits a six-month, 20-stock universe. At scale you would fit it on a
  rolling history and persist the fitted model, which `models/isolation_forest.joblib`
  already supports.
