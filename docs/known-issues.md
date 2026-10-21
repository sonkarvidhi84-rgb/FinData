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

<!-- Further issues found during validation and quality runs are appended below. -->
