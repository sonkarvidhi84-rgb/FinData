# CLAUDE.md — FinData Quality Pipeline

This file is read automatically by Claude Code at the start of every session. It describes the project, how it is built, and the rules you must follow. Read `AGENT.md` next for the phased build plan and data sources.

## 1. What this project is

**FinData Quality Pipeline** is a portfolio project that acquires, validates, models and monitors financial market data for Indian and global securities, exposing a data-quality dashboard. It is built to demonstrate the skills in a *Data Management Professional* role: data acquisition, validation, modelling, quality measurement (statistics + light ML), visualization and clear documentation.

Think of it as a miniature "data product": every dataset must be well-modelled, documented, and have measurable quality.

Target end state:

```
Public sources ──► ingest/ ──► raw (parquet) ──► validate/ ──► staging ──► model/ ──► PostgreSQL/SQLite (star-ish schema)
                                                                                  │
                                                   quality/ (metrics + anomaly detection) ◄┘
                                                                                  │
                                                            dashboard/ (Streamlit) ◄┘
```

## 2. Tech stack (do not deviate without asking)

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Package/env | `uv` (fallback: `pip` + `venv`); `pyproject.toml` is the single source of truth |
| Data handling | `pandas`, `pyarrow` (parquet), `polars` optional for large loads |
| Validation | `pandera` for schema checks, custom rules in `src/findata/validate/rules.py` |
| Storage | SQLite for local dev (`data/findata.db`); PostgreSQL via `DATABASE_URL` for "prod". Use SQLAlchemy 2.x Core + Alembic migrations |
| Orchestration | Plain Python CLI (`typer`) first; `prefect` only in Phase 5 if time allows |
| Quality / ML | `scipy`, `scikit-learn` (IsolationForest, z-scores), `statsmodels` |
| Dashboard | `streamlit` + `plotly` |
| Testing | `pytest`, `pytest-cov`, `hypothesis` for property tests on validators |
| Lint/format | `ruff` (lint + format), `mypy --strict` on `src/` |
| Config | `pydantic-settings`, `.env` (never committed), `config/*.yaml` for dataset definitions |
| Docs | `mkdocs-material`; data dictionary auto-generated from the SQLAlchemy models |

## 3. Repository layout

```
.
├── CLAUDE.md                 # this file
├── AGENT.md                  # build plan, phases, data sources
├── README.md                 # user-facing overview (write last, keep honest)
├── pyproject.toml
├── .env.example
├── config/
│   └── datasets/*.yaml       # one file per dataset: source, schedule, schema, quality thresholds
├── src/findata/
│   ├── ingest/               # one module per source (nse.py, yfinance.py, rbi.py, fred.py, openfigi.py, ...)
│   ├── validate/             # pandera schemas + rule engine
│   ├── model/                # SQLAlchemy models, loaders, dimension/fact builders
│   ├── quality/              # metrics (completeness, timeliness, accuracy, consistency), anomaly detection
│   ├── analytics/            # cross-dataset queries used by the dashboard
│   ├── cli.py                # `findata ingest|validate|load|quality|dashboard`
│   └── settings.py
├── dashboard/app.py
├── migrations/               # alembic
├── data/
│   ├── raw/                  # gitignored, parquet, partitioned by source/date
│   ├── staging/
│   └── samples/              # small committed CSVs (<1 MB) so tests and demos work offline
├── notebooks/                # exploration only; nothing production lives here
├── tests/
└── docs/                     # mkdocs: data dictionary, lineage, known issues, ADRs
```

## 4. Commands

```bash
uv sync                          # install
uv run findata ingest nse --from 2023-01-01
uv run findata validate nse_prices
uv run findata load               # staging -> warehouse
uv run findata quality run        # compute metrics, write to quality tables
uv run streamlit run dashboard/app.py
uv run pytest -q                  # must pass before any commit
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

## 5. Non-negotiable rules

1. **Never commit secrets or raw data.** API keys live in `.env`; `data/raw` and `data/staging` are gitignored. Only `data/samples/` is committed.
2. **Idempotent ingestion.** Re-running an ingest for the same date range must not create duplicates. Use natural keys + upsert.
3. **Every dataset has a YAML definition** in `config/datasets/` *before* any code touches it. The YAML drives the pandera schema, the quality thresholds and the data dictionary.
4. **Every validation rule is a pure function** with a docstring stating *what financial problem it catches* (e.g. "price jumps >40% not explained by a corporate action"). Rules return structured `Anomaly` records, never raise.
5. **Tests first for validators.** A rule without a test (including a passing and a failing fixture) is not done.
6. **Offline-first demos.** The dashboard and test suite must run with only `data/samples/` present. Network calls are behind the `ingest` layer and mocked in tests.
7. **Respect source terms.** Rate-limit every scraper/API client (`tenacity` + explicit sleeps), set a descriptive `User-Agent`, cache responses in `data/raw`. Never hammer NSE/BSE endpoints; prefer official bulk downloads where they exist.
8. **Type everything.** `mypy --strict` must pass on `src/`.
9. **Small, reviewable commits.** One phase step per commit, conventional commit messages (`feat(ingest): add NSE bhavcopy loader`).
10. **Document as you go.** When a dataset, rule or table is added, update `docs/` in the same commit. The "Known issues" page is as important as the features page.
11. **Ask before** adding a dependency, changing the schema of an existing table, or swapping a data source.

## 6. Data modelling conventions

- Surrogate integer keys for dimensions, natural keys enforced with unique constraints.
- Core dimensions: `dim_security`, `dim_issuer`, `dim_exchange`, `dim_calendar`, `dim_currency`.
- Core facts: `fact_price_daily`, `fact_corporate_action`, `fact_fundamental`, `fact_macro_series`, `fact_fx_rate`.
- Identifier mapping table `map_security_identifier` (ISIN, ticker, FIGI, LEI, internal id) with `valid_from`/`valid_to` — identifiers change, model that explicitly.
- Quality tables: `dq_metric` (dataset, metric, value, run_ts), `dq_anomaly` (dataset, record key, rule, severity, detail, status).
- All timestamps UTC; trading dates stored as `DATE`; keep `source_ts` and `load_ts` on every fact.
- Prices stored unadjusted + a separate adjustment-factor table; adjusted series are computed, never stored as the only copy.

## 7. Quality framework (what "quality" means here)

| Dimension | Example metric |
|---|---|
| Completeness | % of expected (security × trading day) rows present |
| Timeliness | Lag between market close and availability in warehouse |
| Accuracy | % of prices within tolerance of a second source (e.g. NSE vs yfinance) |
| Consistency | ISIN↔ticker mappings agree across sources; OHLC sanity (low ≤ open/close ≤ high) |
| Validity | Schema conformance via pandera |
| Uniqueness | Duplicate natural keys |

Anomaly detection layers: (1) hard rules, (2) statistical (rolling z-score on returns, volume), (3) `IsolationForest` on engineered features. Every flag has a severity and a human-readable explanation.

## 8. Style for written output

- Docstrings: Google style. Explain the *financial* reasoning, not just the code.
- README and docs: concise, no marketing language, include an honest "limitations" section.
- Dashboard: dark/light safe, minimal, three tabs — Quality Overview, Anomalies, Analytics.

## 9. Working with the human

The owner is a final-year Maths & Computing student preparing for a data-management interview. When you make a design choice, add a one-paragraph note to `docs/adr/` explaining the trade-off in interview-ready language. Prefer explaining over magic. If something is ambiguous, pick the simplest option that keeps rules 1–11 intact and say what you assumed.
