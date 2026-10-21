# Data sources

Every source is reached only through `src/findata/ingest/`. Each one goes through
`PoliteSession`, which applies a per-host minimum interval, a descriptive User-Agent, and
`tenacity` retries with exponential back-off on 429/5xx. Raw responses are cached under
`data/raw/` (gitignored). The committed `data/samples/*.csv` are real data truncated to 20
NIFTY-50 names and 2024-01-01 to 2024-06-30.

"Terms verified" means we fetched the terms page during development (Oct 2026) and read the
relevant line. "Not verified" means we didn't, so treat the note as our understanding only.

| Dataset | Source / endpoint | Key | Our rate limit | Terms |
|---|---|---|---|---|
| `nse_prices` | NSE UDiFF bhavcopy `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip` | No | 1 req/s, zips cached forever, 404s cached | Not verified. NSE website terms restrict redistribution of NSE data. We commit only a small sample for a non-commercial portfolio project. |
| `nse_corporate_actions` | `www.nseindia.com/api/corporates-corporateActions?index=equities&from_date&to_date` (needs a cookie from the HTML page first) | No | ≥2 s between calls; one call per range | Not verified (same NSE terms). |
| `nse_securities` | `nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | No | 1 call per ingest | Not verified (same NSE terms). |
| `yahoo_prices` | Yahoo Finance via `yfinance` (unofficial) | No | One ticker per request, 1.5 s pause | Not verified. Yahoo's terms prohibit redistribution, and the library is unofficial and may break. Used only as a cross-check. |
| `fx_rates` | FBIL `www.fbil.org.in/wasdm/refrates/fetchfiltered?fromDate&toDate` | No | 1 req/s, 30-day chunks | Not verified. FBIL has administered the INR reference rate since July 2018, when it took over from RBI. AGENT.md asks for "RBI reference FX rates", and this is the same benchmark. |
| `fred_macro` | FRED keyless CSV `fred.stlouisfed.org/graph/fredgraph.csv?id=<series>&cosd&coed` | No (optional `FRED_API_KEY` not used yet) | 1 req/s | Not verified in this session. FRED states that some series are third-party copyrighted. The ones used here (CPI, DFF, DGS10, VIXCLS, DEXINUS) are public US-government or Cboe series; check VIXCLS before any redistribution. |
| `worldbank_macro` | World Bank v2 `api.worldbank.org/v2/country/IND/indicator/<code>?format=json` | No | 1 req/s | Not verified in this session (World Bank datasets are generally CC BY 4.0). |
| `openfigi_mapping` | OpenFIGI v3 `api.openfigi.com/v3/mapping` (POST, ≤10 jobs) | Optional `OPENFIGI_API_KEY` | 2.5 s between calls (≤24/min) | Verified that the docs list a 25/min (no key) and 250/min (key) mapping limit. FIGI is an open OMG standard originated by Bloomberg. |
| `gleif_lei` | GLEIF `api.gleif.org/api/v1/lei-records` and `/lei-records/<lei>/direct-parent` | No | 1 req/s | Verified: GLEIF data is published under CC0. |

## Source-specific notes

- **NSE calendar.** `config/calendars/nse.yaml` is *derived*, not hand-typed. A weekday on which
  NSE published no bhavcopy is a holiday, and a weekend day with a bhavcopy is a special session.
  NSE's `holiday-master` API only returns the current year, so 2024 holidays carry the generic
  name "No session (no bhavcopy published)" and no names recalled from memory.
- **Bhavcopy timeliness.** `source_ts` is the HTTP `Last-Modified` of the zip. NSE re-generates
  archive files from time to time, so for historical dates this can be later than the original
  publication, and timeliness is only meaningful for live daily runs (see known issues).
- **Corporate-action subjects** are free text. `parse_subject` classifies them into
  dividend / split / bonus / rights / buyback / other, sums combined dividends
  ("Interim ... & Special ..."), and derives a price ratio for splits (new FV / old FV) and
  bonuses (b / (a + b) for "Bonus a:b"). InvIT/REIT "Distribution" rows count as dividends.
- **Yahoo** `Close` is split-adjusted, but not dividend-adjusted, *retroactively*. NSE prices
  are unadjusted, so before a split the two disagree by the split ratio. Cross-checks must put
  both on the same basis.
- **FRED / World Bank revisions.** Each fetch is stamped with `vintage_date` (the day it was
  fetched), and raw partitions are keyed by vintage, so revisions accumulate instead of
  overwriting. World Bank fetches use a 10-year look-back, because annual data for the current
  year is usually not published yet.
- **OpenFIGI.** One ISIN maps to a FIGI per venue (IN = India composite, IS = NSE, IB = BSE).
  We store the India composite.
- **GLEIF entity resolution.** In Oct 2026, GLEIF's `filter[isin]` returned nothing for the
  Indian ISINs we tried, because ISIN-to-LEI links come from ANNA and coverage is thin. The
  client tries the ISIN first. It then searches Indian entities by the NSE company name and
  accepts only an *exact* normalised legal-name match (case-insensitive; LTD becomes LIMITED,
  & becomes AND). Fuzzy hits such as "RELIANCE INDUSTRIES LIMITED EMPLOYEES GRATUITY FUND" are
  rejected, because we prefer a missing LEI to a wrong one. The dataset YAML's `source_url`
  still describes the ISIN filter only.

## Not implemented (yet)

BSE bhavcopy and scrip master, MOSPI / data.gov.in, SEC EDGAR, Alpha Vantage / Twelve Data /
Finnhub, Stooq, Kaggle, Nasdaq Data Link, niftyindices constituents (sectors are a static
snapshot in `config/universe.yaml`), and Screener/BSE fundamentals.
