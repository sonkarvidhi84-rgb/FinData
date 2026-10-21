# AGENT.md — Build plan, data sources and acceptance criteria

Read `CLAUDE.md` first. This file is the execution plan. Work through the phases in order; each phase ends with a green test suite, updated docs and a commit. Do not start a phase until the previous one's acceptance criteria are met.

Total scope is sized for ~5–6 weeks of part-time work. Phase 5 is optional.

---

## Phase 0 — Bootstrap (day 1)

**Do**
- `uv init`, create `pyproject.toml` with the stack from CLAUDE.md §2, pin versions.
- Create the directory layout from CLAUDE.md §3 with `__init__.py` files and empty `README.md`s where helpful.
- `.gitignore` (data/raw, data/staging, .env, *.db, .venv, notebooks/*.ipynb_checkpoints).
- `.env.example` with every key listed in the data-source table below (empty values).
- `src/findata/settings.py` (pydantic-settings) reading `DATABASE_URL`, API keys, `DATA_DIR`.
- `cli.py` with stub commands.
- Pre-commit config: ruff, mypy, pytest.
- Mkdocs skeleton: `docs/index.md`, `docs/data-dictionary.md`, `docs/lineage.md`, `docs/known-issues.md`, `docs/adr/0001-storage-choice.md`.

**Accept when**: `uv run pytest` passes (even with one trivial test), `ruff`/`mypy` clean, `findata --help` lists commands.

---

## Phase 1 — Acquisition (week 1–2)

Implement one client per source under `src/findata/ingest/`. Each client exposes `fetch(start: date, end: date, symbols: list[str] | None) -> pd.DataFrame` and writes parquet to `data/raw/<source>/<dataset>/dt=<YYYY-MM-DD>/`. Add a `config/datasets/<name>.yaml` for each dataset before writing the client.

Order of implementation (do the first three before anything else):

1. **NSE bhavcopy** (daily equity OHLCV, all listed stocks) — bulk CSV, no key.
2. **NSE corporate actions** (splits, bonuses, dividends, record dates).
3. **yfinance** for the same tickers (`.NS` suffix) — this is the *second source* used for accuracy cross-checks.
4. **RBI reference FX rates** (USD/INR etc.).
5. **FRED** macro series (US CPI, Fed funds, 10y yield) — needs free key.
6. **World Bank / MOSPI** India macro (GDP, CPI, IIP).
7. **OpenFIGI** — map ISIN/ticker → FIGI. This is Bloomberg's own open identifier standard; worth highlighting in the README.
8. **GLEIF LEI** — issuer identifiers and legal-entity hierarchy (bulk file).
9. **Screener/BSE fundamentals** (quarterly revenue, EPS) — optional, lowest priority; use only official/bulk endpoints.

Also create `data/samples/` with ~20 NIFTY-50 tickers × 6 months for every dataset so tests/dashboard run offline. Generate them by running the clients once and truncating.

**Accept when**: every client has a mocked unit test, respects rate limits, is idempotent, and `findata ingest all --from 2024-01-01 --to 2024-06-30` populates `data/raw`.

---

## Phase 2 — Validation (week 2–3)

- `src/findata/validate/schemas.py`: pandera `DataFrameModel` per dataset, generated from YAML where possible.
- `src/findata/validate/rules.py`: rule engine. Each rule: `def rule_name(df: pd.DataFrame, ctx: RuleContext) -> list[Anomaly]`.
- Minimum rule set (each with passing + failing fixture tests):
  - `ohlc_sanity` — low ≤ min(open, close), high ≥ max(open, close), all > 0.
  - `missing_trading_days` — against `dim_calendar` (NSE holiday list in `config/calendars/nse.yaml`).
  - `duplicate_keys` — (isin, trade_date) uniqueness.
  - `stale_price` — identical close for N consecutive sessions with non-zero volume.
  - `unexplained_jump` — |return| > threshold with no corporate action within ±2 days.
  - `split_adjustment_check` — price ratio around a split matches the declared ratio within tolerance.
  - `cross_source_price_diff` — NSE vs yfinance close differ by > X% .
  - `identifier_conflict` — same ticker mapped to two ISINs in the same period.
  - `fx_rate_outlier` — daily FX move beyond rolling z-score.
  - `macro_revision_detected` — a previously loaded macro value changed (store revisions, do not overwrite).
- Anomalies are written to `data/staging/anomalies.parquet` and later to `dq_anomaly`.

**Accept when**: `findata validate all` runs on samples, produces anomalies with severities, and `pytest --cov=src/findata/validate` ≥ 90%.

---

## Phase 3 — Modelling & load (week 3–4)

- SQLAlchemy models for the tables in CLAUDE.md §6; Alembic initial migration.
- Loaders: staging → dimensions (upsert) → facts (upsert on natural key).
- Adjustment-factor builder from corporate actions; `adjusted_close` as a SQL view.
- `map_security_identifier` populated from NSE + OpenFIGI + GLEIF with validity windows.
- `docs/data-dictionary.md` auto-generated from models (`scripts/gen_data_dictionary.py`); `docs/lineage.md` with a Mermaid diagram source → table.
- Three analytical queries in `analytics/` that join across datasets, e.g.:
  - sector-adjusted 5-day returns around dividend record dates;
  - INR-hedged vs unhedged returns using RBI FX;
  - correlation of NIFTY-50 constituent returns with US 10y yield changes.

**Accept when**: fresh clone → `findata load` on samples builds the DB; the three queries return sane numbers; schema diagram renders in docs.

---

## Phase 4 — Quality metrics, anomaly ML, dashboard (week 4–5)

- `quality/metrics.py`: completeness, timeliness, accuracy, consistency, validity, uniqueness per dataset per run → `dq_metric`.
- `quality/anomaly.py`: rolling z-scores (returns, volume), `IsolationForest` on [return, volume ratio, spread, cross-source diff]; calibrate contamination on sample data; persist model with `joblib` to `models/`.
- `quality/report.py`: markdown summary of latest run, appended to `docs/quality-runs/`.
- `dashboard/app.py` (Streamlit, three tabs):
  1. **Quality Overview** — metric time series per dataset, traffic-light table, last run time.
  2. **Anomalies** — filterable table, click to see the price chart with the flagged point and any nearby corporate action; "mark as reviewed" writes to `dq_anomaly.status`.
  3. **Analytics** — the three cross-dataset queries as Plotly charts.
- Add a "Methodology" expander explaining each metric in plain language.

**Accept when**: dashboard runs offline on samples, all tabs populated, anomaly precision on a hand-labelled set of 50 flags is recorded in `docs/quality-evaluation.md`.

---

## Phase 5 — Polish / stretch (week 6, optional)

- `prefect` flow with daily schedule; GitHub Actions for lint/test + nightly ingest on samples.
- Great Expectations or `soda-core` comparison note in an ADR (why we chose pandera).
- Docker compose with PostgreSQL.
- A 2-page `docs/case-study.md`: problem, architecture, one real anomaly you caught, what you'd do next. This is the interview artefact.

---

## Data sources catalogue

Verify each endpoint before relying on it: APIs change, and some of these have usage caps or terms that restrict redistribution. Record the terms you read in `docs/data-sources.md`. Prefer bulk/official downloads over scraping.

| # | Source | Data | Access | Key needed | Notes |
|---|---|---|---|---|---|
| 1 | NSE India — bhavcopy & reports (`nseindia.com`, "All Reports" section) | Daily equity OHLCV for all listed securities, index constituents, holiday list | Bulk CSV/ZIP per day | No | Set browser-like headers and a cookie session; rate-limit hard. Python wrappers: `jugaad-data`, `nsepython` (check maintenance status). |
| 2 | NSE corporate actions | Splits, bonus, dividend, rights, record/ex dates | CSV export from the corporate-actions page/API | No | Needed for `unexplained_jump` and adjustment factors. |
| 3 | BSE India (`bseindia.com`) | Bhavcopy, corporate announcements, scrip master with ISIN | Bulk CSV | No | Good second Indian source; scrip master gives ISIN↔code. |
| 4 | Yahoo Finance via `yfinance` | OHLCV, adjusted close, basic fundamentals | Python lib | No | Use `.NS`/`.BO` suffixes. Unofficial; treat as cross-check only and expect breakage. |
| 5 | RBI DBIE (`dbie.rbi.org.in`) & RBI reference rates | USD/INR, EUR/INR, GBP/INR, JPY/INR reference rates; policy rates; money supply | Excel/CSV downloads | No | Reference rates are published each business day. |
| 6 | FRED (`fred.stlouisfed.org`) | US & global macro: CPI, Fed funds, treasury yields, VIX | REST API, `fredapi` | Yes (free) | Thousands of series; clean, revision-aware. |
| 7 | World Bank Indicators API | India & global GDP, inflation, population | REST, `wbgapi` | No | Annual granularity. |
| 8 | MOSPI / data.gov.in | India CPI, IIP, GDP quarterly | CSV / API | data.gov.in key (free) | Formats inconsistent; good "messy data" showcase. |
| 9 | OpenFIGI (`openfigi.com/api`) | Map ISIN/ticker/SEDOL → FIGI, security type, exchange | REST | Optional key raises rate limit | Bloomberg's open symbology — call this out explicitly. |
| 10 | GLEIF (`gleif.org`) | LEI records, legal names, parent/child relationships | Bulk JSON/CSV, REST | No | Use for `dim_issuer` and entity resolution. |
| 11 | SEC EDGAR (`sec.gov`) | US company facts (XBRL financials), filings | REST (company facts JSON) | No, but requires `User-Agent` with contact email | Optional: add 10 US large-caps for a global flavour. |
| 12 | Alpha Vantage / Twelve Data / Finnhub | Global equities & FX, fundamentals | REST | Yes (free tier) | Backup to yfinance; strict daily call limits. |
| 13 | Stooq (`stooq.com`) | Long-history daily index/FX data | CSV | No | Handy for benchmarks. |
| 14 | Kaggle: "NIFTY 50 Stock Market Data", "Indian Stock Market" datasets | Historical NSE prices | Download | Kaggle account | Only for backfilling history or offline samples; note licence. |
| 15 | Nasdaq Data Link (ex-Quandl) | Selected free macro/commodity series | REST | Yes (free) | Many datasets now paid; check before using. |
| 16 | NSE/BSE index data, NIFTY indices site (`niftyindices.com`) | Index constituents, weights, historical index values | Excel/CSV | No | Needed for sector-adjusted returns. |

### Suggested Python libraries per source
`jugaad-data`, `nsepython`, `yfinance`, `fredapi`, `wbgapi`, `requests` + `tenacity` for everything else, `openpyxl` for RBI/NIFTY Excel files.

### Reference reading (for docstrings and ADRs)
- DAMA-DMBOK data quality dimensions (completeness, timeliness, accuracy, consistency, validity, uniqueness).
- Kimball, *The Data Warehouse Toolkit* — chapters on slowly changing dimensions (used for identifier validity windows).
- pandera docs; scikit-learn `IsolationForest` user guide.
- OpenFIGI API docs and the FIGI standard overview (OMG).
- NSE "Corporate Actions" help page — definitions of ex-date vs record date.

---

## Definition of done for the whole project

- `git clone` → `uv sync` → `uv run findata load` → `uv run streamlit run dashboard/app.py` works offline in under 5 minutes.
- README has: architecture diagram, 3 screenshots, how to run, data sources & terms, limitations, "what I learned".
- `docs/` has data dictionary, lineage, ≥ 5 ADRs, quality evaluation, known issues.
- Test coverage ≥ 85% on `src/`.
- At least one real, documented anomaly found in live data (not samples), with the explanation of what caused it.
