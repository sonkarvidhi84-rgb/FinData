# Validation rules

Validation runs in two layers:

1. **Schema (validity).** `src/findata/validate/schemas.py` turns each dataset YAML into a
   pandera `DataFrameSchema`, covering dtype, nullability, `gt`/`ge`, regex `pattern` (ISIN,
   FIGI, LEI) and `isin_set`, plus natural-key uniqueness. Failing rows become
   `schema_validity` anomalies and are left out of staging.
2. **Rules.** `src/findata/validate/rules.py` holds pure functions,
   `rule(df, ctx) -> list[Anomaly]`, that never raise. Which rules run, and with which
   parameters, is set by the `rules:` block of each dataset YAML.

Each anomaly carries `dataset`, `rule`, `severity`, `record_key` (`SYMBOL|YYYY-MM-DD`,
`SERIES|date` or `CCY|date`), a human-readable `detail`, and the numeric `value` that
triggered it. `findata validate` writes them to `data/staging/anomalies.parquet`; `findata load`
then copies them into `dq_anomaly`.

| Rule | Financial problem it catches | Default severity | Params (YAML) |
|---|---|---|---|
| `schema_validity` | Malformed rows: bad ISIN, negative price, wrong type, duplicate natural key | high (medium for uniqueness) | from `columns:` |
| `ohlc_sanity` | Impossible bars: low > min(open, close), high < max(open, close), price ≤ 0. Signals feed corruption or column swaps, which poison returns and VaR | high | – |
| `missing_trading_days` | An expected NSE session with no row, which silently turns the next return into a two-day return. Checked only inside each symbol's first..last date | medium | calendar `config/calendars/nse.yaml` |
| `duplicate_keys` | The same natural key twice: double-counted volume or market value, and an ambiguous "official" price | high | key = YAML `natural_key` (+ `vintage_date`) |
| `stale_price` | A close unchanged for N sessions while shares trade, meaning a stuck feed that hides volatility and losses | medium | `min_sessions` |
| `unexplained_jump` | A move of more than X% with no split, bonus or rights issue within ±N days. Either real news to confirm or a bad tick | high | `threshold`, `window_days` |
| `split_adjustment_check` | The price ratio across an ex-date doesn't match the declared split/bonus ratio, so either the declared ratio or the ex-date is wrong. Also flags a declared split with no prices around it | high | `tolerance` |
| `cross_source_price_diff` | NSE and Yahoo disagree on the daily return by more than X pp (over common dates, after split adjustment); also emits `cross_source_basis` notes. Measures **accuracy** without a "true" price | medium (basis: low) | `threshold_pct` |
| `identifier_conflict` | One ticker mapped to two ISINs (or one ISIN to two tickers) on the same day, which breaks joins between positions and prices | high | – |
| `identifier_change` | (informational) The ISIN changed between dates, e.g. after a face-value split. The identifier map should open a new validity window | low | – |
| `fx_rate_outlier` | An INR fixing move more than *z* standard deviations away from the previous *window* of daily log-changes. Usually a units error (JPY per 1 vs per 100) or a decimal shift | medium | `window`, `z_threshold` |
| `macro_revision_detected` | A macro value changed since the previously loaded vintage. We store it as a new vintage to avoid look-ahead bias in backtests | low | – |

## Design notes

- **Returns use our own previous close, not NSE's `prev_close`.** NSE publishes `PrvsClsgPric`
  already *adjusted* for corporate actions on the ex-date. Using it would hide exactly the split
  jumps that `unexplained_jump` exists to catch.
- **Dividends rarely explain jumps.** `unexplained_jump` accepts a dividend as the explanation
  only when the cash amount is at least half of the observed move. An ordinary ₹10 dividend
  can't explain a 20% drop.
- **Severity can be overridden** per dataset in YAML (`ohlc_sanity` is `high` for NSE but `low`
  for Yahoo, which is only a cross-check source).
- **Macro vintages.** The engine splits versioned datasets into the *latest* vintage, which the
  rules validate, and the *previous* vintage, which `macro_revision_detected` compares against.
  Staging keeps every vintage.

## Lesson: Yahoo's `Close` is split-adjusted

Comparing NSE's unadjusted bhavcopy close with Yahoo's `Close` on a pre-split date gives a
false "900% error" for a 1:10 split like NESTLEIND in January 2024. Yahoo retroactively rescales
its history after a split, while the exchange file stays as printed on the day.
`split_factor_series(trade_dates, ex_dates, ratios)` computes, for each date, the product of the
ratios of all *later* split/bonus events. Multiplying the NSE close by it puts both sources on
the same share basis before comparing. Dividends aren't adjusted on either side: Yahoo's
`Close` excludes dividend adjustment, and its `Adj Close` includes it.

The general point: before measuring accuracy between two sources, make sure they mean the
same thing by "close".

### Second lesson, found on the real 2024 data: Yahoo restates for *future* actions too

Split-adjusting NSE with our own in-window corporate actions was not enough. Yahoo had also
restated the whole H1-2024 history for actions that happened **after** the window. Its close
was 0.5x NSE for HDFCBANK, RELIANCE and NESTLEIND (later bonuses or splits), 0.2x for KOTAKBANK
and 0.9629x for ITC (the 2025 ITC Hotels demerger). The first version of the rule therefore
flagged 600 of 2,460 rows. Nearly all of them were false positives.

The rule now compares **daily returns over dates present in both sources**, which don't change
when the price level is rescaled (`cross_source_alignment`). The level ratio is reported once per
symbol as a low-severity `cross_source_basis` note. After the change the rule flags 0 rows on the
samples, plus 5 basis notes. A missed split still shows up, on its ex-date, as a return gap.

Known blind spot: if a vendor is wrong by a constant factor for the *whole* window (for example
a wrong-security mapping that happens to move in step), a returns comparison cannot see it. The
basis note is how that would surface.
