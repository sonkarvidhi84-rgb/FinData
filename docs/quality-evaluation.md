# Quality evaluation: how precise are the anomaly flags?

A detector that cries wolf costs analyst time and gets ignored. We estimate **precision**, the
share of raised flags that deserved a look, on a sample of 50 flags. Recall cannot be measured
without a fully labelled history and is out of scope (see Known issues).

## Method

1. **Sample.** `scripts/eval_flags.py` draws 50 flags from `dq_anomaly`, stratified by
   detector (`rule`, `statistical`, `ml`): an equal share per detector, topped up at random if
   a detector has fewer flags. The seed is fixed (`--seed 7`) so the sample is reproducible.
2. **Evidence.** For each flag the script collects checkable facts from the warehouse:
   - NSE return (split-adjusted) and Yahoo return on the same date;
   - |NSE - Yahoo| close difference after split adjustment;
   - volume / trailing 20-session median;
   - OHLC consistency of the NSE row;
   - corporate actions with an ex-date within ±3 calendar days.
3. **Label.** The criteria are applied in this order:

   | Label | Criterion | Counts as useful? | Counts as data defect? |
   |---|---|---|---|
   | `data_defect` | OHLC inconsistent, **or** NSE vs Yahoo differ > 0.5% after split adjustment, **or** duplicate natural key, **or** expected session missing | yes | yes |
   | `explained` | corporate action within ±3 days and the move is modest (< 5%, volume < 3x) | no | no |
   | `market_event` | \|return\| ≥ 5% or volume ≥ 3x median. If Yahoo's return agrees within 1pp, the move is real | yes | no |
   | `noise` | none of the above | no | no |
   | `unverifiable` | non-price record with no objective check (e.g. identifier conflicts) | excluded | excluded |

4. **Precision** is reported two ways:
   - *useful precision* = (data_defect + market_event) / labelled flags. Would an analyst
     want to see it?
   - *defect precision* = data_defect / labelled flags. Did it find an actual data error?

> **Who labelled these?** The AI agent that built this project wrote the criteria above, and
> the labels are produced by applying them mechanically. Nobody has labelled the flags by
> hand. The criteria are only a proxy: a second source agreeing with NSE does not prove the
> price is right, and a large move can be a genuine event. **The owner should re-label the
> sheet by hand**, using the empty `human_label` column in
> `docs/quality-runs/eval_flags_sheet.csv`, before quoting these numbers.

## Results (committed samples: 20 NIFTY-50 stocks, 2024-01-01 to 2024-06-30, real NSE/Yahoo data)

Run `20261021T100550Z` ([report](quality-runs/20261021T100550Z.md), sheet
[`eval_flags_sheet.csv`](quality-runs/eval_flags_sheet.csv)). There were exactly 50 flags in
`dq_anomaly` (8 rule, 37 statistical, 5 ML), so the "sample" **is the whole population** for
this period.

| Detector | Flags | data_defect | market_event | explained | noise | informational / unverifiable | Useful precision | Useful precision excl. informational |
|---|---|---|---|---|---|---|---|---|
| ml (IsolationForest) | 5 | 0 | 5 | 0 | 0 | 0 | **1.00** | 1.00 |
| statistical (z-score) | 37 | 0 | 32 | 0 | 5 | 0 | **0.86** | 0.86 |
| rule | 8 | 0 | 1 | 1 | 0 | 6 (5 basis notes, 1 FX outlier) | 0.12 | 0.50 |
| **all** | 50 | 0 | 38 | 1 | 5 | 6 | **0.76** | 0.86 |

**What this says**

- **Defect precision is 0 for every detector.** In this window, NSE bhavcopy and Yahoo agree on
  every daily return to within 0.5pp once splits are applied (accuracy metric 100%). No flag
  points at a wrong price. Official exchange end-of-day data is clean, so the detectors are
  surfacing *market events*, not *data errors*. This is a property of the data, not proof
  that the detectors can find errors. The synthetic-defect unit tests cover that.
- **The flags cluster on real events.** 2024-06-04 (Lok Sabha election results: SBIN -14%,
  NTPC -15%, LT -13% on 3-8x volume) produced every ML flag and the only `unexplained_jump`.
  2024-06-03 (exit-poll rally) produced several more. Other clusters are KOTAKBANK on
  2024-04-25 (-10.9% on 12x volume, the day after the RBI's restrictions on the bank),
  HDFCBANK on 2024-01-17 (-8.4% after Q3 results) and TITAN on 2024-05-06 (-7.2% after Q4
  results). *The event attributions are public market context added by the agent; the
  pipeline does not verify them.*
- **The 5 "noise" flags** are 4-5 sigma moves of 2-5% for low-volatility stocks. They are
  statistically unusual for that stock but below the 5% / 3x-volume bar used here. A human
  might reasonably call some of them useful.
- **Rule-layer precision is low only because of informational flags.** The 5
  `cross_source_basis` notes are by design one-per-symbol notices that Yahoo has restated
  history (see Known issues), not per-row alarms.

**A defect the evaluation found in our own detector.** The first evaluation pass found that
41 of the then-72 statistical flags were thin volume on NSE's Saturday special sessions
(2024-01-20, 2024-03-02, 2024-05-18: DR-site drills that trade 2-8% of normal volume). Those
sessions are now excluded from the volume z-score and from its trailing window, and stale
open statistical/ML flags are retired on re-run. Statistical flags fell from 72 to 37 with
no loss of genuine events. An earlier version also compared price *levels* across sources.
That flagged every row of the 5 restated symbols, and it was replaced by return-based
alignment.

**Caveats.** n = 50, which gives roughly ±0.12 on a proportion at 95%. The criteria are
the agent's. A human should fill in `human_label` and recompute. Recall is not estimated.

Re-run with:

```bash
uv run findata load && uv run findata quality run
uv run python scripts/eval_flags.py --n 50
```
