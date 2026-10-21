# ADR 0001 — SQLite locally, PostgreSQL in "prod", via SQLAlchemy Core + Alembic

**Status:** accepted

**Context.** The project has to run offline from a fresh clone within minutes, but should also
show it could serve a team. **Decision.** All table definitions live once in SQLAlchemy Core
`MetaData`. Alembic migrations are the only way the schema changes. The engine URL comes from
`DATABASE_URL`: empty means a file at `data/findata.db`, and a `postgresql+psycopg://` URL
targets Postgres. Upserts go through a single helper that dispatches on dialect, because
`INSERT ... ON CONFLICT DO UPDATE` has different import paths on the two backends.
**Trade-off.** SQLite has no real `DATE` type and no concurrent writers. That suits a
single-user demo but not a scheduled multi-writer pipeline. We give up some Postgres-only
features (partial indexes, `MERGE`) in exchange for zero setup. We chose Core over the ORM
because loads are set-based (DataFrame → bulk upsert), and per-row identity tracking adds
overhead without benefit.
