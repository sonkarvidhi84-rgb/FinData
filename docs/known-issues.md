# Known issues

Data products fail in quiet ways. This page lists every limitation we know of, so nobody
discovers one in an interview or in production.

## Sources

| # | Issue | Impact | Mitigation |
|---|---|---|---|
| S1 | Sector labels are a **2026-10-21 snapshot** of the NIFTY 50 constituent file, applied to 2024 prices. | Sector-adjusted returns use today's classification, not the point-in-time one. | Store sector with validity windows (SCD2) once a historical source is wired in. |
| S2 | The NSE holiday calendar is **derived from bhavcopy availability** (no file means no session); NSE's API only serves the current year. | A day NSE failed to publish would be counted as a holiday. | Cross-check against the NSE holiday circulars when they can be fetched. |
| S3 | Yahoo Finance (`yfinance`) is unofficial and its `Close` is **split-adjusted**. | Naive NSE-vs-Yahoo comparisons flag every pre-split day. | `cross_source_price_diff` rescales NSE closes by later split/bonus ratios before comparing. |
| S4 | The NSE corporate-actions endpoint is an undocumented JSON API behind a cookie session. | It can break without notice. | Responses are cached in `data/raw`; the client fails loudly and the rest of the pipeline continues. |
| S5 | Corporate-action amounts and ratios are **parsed from free text** (`subject`). | Unusual wording falls through to `action_type = other`. | Every unparsed subject is visible in staging; parser cases are unit-tested. |
| S6 | FRED/World Bank revisions are only detected **between our own pulls** (keyless endpoint, no ALFRED vintages). | Revisions made before the first pull are invisible. | Use ALFRED with an API key for full vintage history. |
| S7 | Fundamentals (Screener/BSE) are **not ingested**. | `fact_fundamental` is empty. | Deferred; see ADR 0006. |

## Modelling

| # | Issue | Impact |
|---|---|---|
| M1 | Dividend adjustment uses the previous session's close (CRSP style) and ignores tax and record-date nuances. | Adjusted returns around ex-dates are approximate. |
| M2 | Rights issues and buybacks are recorded but **not** turned into adjustment factors. | Adjusted series ignore rights dilution. |
| M3 | SQLite has no native `DATE` type, and loads have a single writer. | Use PostgreSQL (`DATABASE_URL`) for anything multi-user. |

## Found while running the pipeline on real data (2026-10-21)

| # | Issue | Impact | Status / mitigation |
|---|---|---|---|
| R1 | **Yahoo restates history for corporate actions *after* our window.** Its close is 0.5x NSE for HDFCBANK, RELIANCE and NESTLEIND, 0.2x for KOTAKBANK and 0.9629x for ITC. | The first, level-based cross-source rule flagged 600 of 2,460 rows. | Fixed: the comparison now runs on returns over common dates and the level ratio is reported as `cross_source_basis`. Corporate actions are only ingested for the window. Fetching them up to today would let the basis be *explained*, not just measured. |
| R2 | **Yahoo has no rows for NSE's Saturday special sessions** (2024-01-20, 03-02, 05-18). | Yahoo completeness 97.56% (WARN); those 60 NSE rows have no second source. | Accepted; the WARN is the correct signal. |
| R3 | **Saturday special sessions trade 2-8% of normal volume.** | The volume z-score flagged them all (41 of 72 statistical flags). | Fixed: excluded from the volume z-score and its trailing window. |
| R4 | **NSE re-publishes historical bhavcopy files.** Last-Modified on some 2024 files is days after the trade date (e.g. the 2024-01-01 file says 2024-01-05). | Timeliness p95 (7.3 h) on a historical backfill partly measures re-publication, not original latency. | Timeliness is only meaningful for daily incremental runs; noted on the metric. |
| R5 | **FX timeliness is 0 h by construction**: FBIL's `displayTime` is used both as fixing time and as `source_ts`. | The metric cannot fail. | Use HTTP Last-Modified or our own first-seen time for FX. |
| R6 | **NSE's `prev_close` on an ex-date is the unadjusted prior close** (NESTLEIND 27,116.40 vs close 2,666.40). | A return computed from `prev_close` shows about -90% on a split. | Rules compute returns from our own previous close plus corporate actions, never from `prev_close`. |
| R7 | **The NSE equity master is a current snapshot.** KOTAKBANK traded in 2024 under INE237A01028, but today's master says INE237A01036. | Joining 2024 prices to today's master by ISIN silently drops rows. | ISIN validity windows come from the price history (`map_security_identifier`), not from the master. |
| R8 | **OpenFIGI does not map retired ISINs** (NESTLEIND INE239A01016). | FIGI coverage is by current ISIN only. | FIGI is attached at the listing level (`dim_security`), which is stable across the ISIN change. |
| R9 | **GLEIF `filter[isin]` returns nothing for Indian ISINs.** | LEIs are found by an exact normalised legal-name match instead (20/20 matched). | Name matching can fail on renames; unmatched issuers stay without an LEI rather than being guessed. |
| R10 | **No data defects in the 20-stock sample.** Every flag there is a market event (e.g. 2024-06-04 election results). | Defect precision cannot be estimated from the sample. | Detectors are tested on synthetic defects; the full raw universe *did* expose a pipeline defect (R13). |
| R11 | **The flag precision labels were produced by the AI agent** applying written criteria, not by a human. | The precision numbers are provisional. | Re-label `docs/quality-runs/eval_flags_sheet.csv` (`human_label`). |
| R12 | Headless capture of the dashboard only rendered the Streamlit loading skeleton. | Screenshots had to be taken by hand; the Quality Overview capture comes from a run made during a World Bank outage (28 pass rather than 31). | Fixed: manual screenshots are in `docs/img/` and embedded in the README. |
| R13 | **`nse_prices` keeps only series EQ.** Stocks moved to the BE (trade-for-trade) surveillance series appear to stop trading. Found in live data: 7,860 of 8,048 full-universe `missing_trading_days` flags (158 symbols, e.g. 3PLAND from 2024-01-31). | Completeness is understated for small caps; there is no impact on the NIFTY-50 sample. | **Open.** Keep EQ+BE+BZ, with series as an attribute; needs a contract change (owner approval). |
| R14 | **`stale_price` flags liquid ETFs** priced at a constant 1,000 by design (e.g. ABSLLIQUID), as well as illiquid penny stocks. | False positives outside the sample. | Open: exempt instrument types once `security_type` is populated for the full universe. |
