# ADR 0006: Source choices and their substitutes

**Status:** accepted

**Context.** AGENT.md lists preferred sources. Before writing a client, each one was probed live
on 2026-10-21, and some had to be adapted.

**Decisions.**

- **INR FX from FBIL, not an RBI page.** RBI stopped publishing the reference rate in July 2018,
  when the Financial Benchmarks India Ltd (FBIL) took over its administration. FBIL's public JSON
  endpoint gives exactly the rate RBI used to publish (USD, GBP, EUR, JPY/100). The data is the
  same; only the publisher changed. FRED's `DEXINUS` (the Fed's H.10 noon-buying rate in New York)
  is also loaded, so INR has a second, independent fixing for cross-checks.
- **FRED through the keyless `fredgraph.csv` endpoint.** The project then runs without an API key.
  `fredapi` and ALFRED vintages need a key, so revisions are tracked by stamping a `vintage_date`
  on every pull and never overwriting older ones.
- **World Bank through plain `requests`** instead of `wbgapi`, which saves a dependency for three
  series.
- **NSE bhavcopy in the UDiFF format.** It has been the standard format since July 2024 and NSE
  also back-filled earlier dates. It carries the ISIN on every row, which the identifier model
  relies on.
- **NSE holiday calendar derived from bhavcopy availability.** NSE's holiday API returns only the
  current year. A day with no published bhavcopy is treated as a non-session. Saturday special
  sessions are caught because every calendar day is probed.
- **Fundamentals (Screener/BSE) deferred.** They are the lowest priority in AGENT.md, and no
  official bulk endpoint was available. `fact_fundamental` exists but is empty.

**Dev-only dependencies added.** These are type stubs needed for `mypy --strict`: `pandas-stubs`,
`types-requests`, `types-PyYAML`. They are not installed at runtime.

**Trade-off.** Every substitute is documented in `docs/data-sources.md`. The cost is reliance on
endpoints without a formal SLA (the NSE JSON API, FBIL, Yahoo). Clients cache raw responses and
fail per day/series instead of failing the whole run.
