"""Engine creation, programmatic Alembic migrations and a dialect-aware upsert helper."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, Table, create_engine, event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection

from findata.settings import PROJECT_ROOT, get_settings

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
BATCH_SIZE = 500


def _enable_sqlite_fk(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def get_engine(url: str | None = None) -> Engine:
    """Create an engine for `url` (default: settings.db_url).

    SQLite does not enforce foreign keys unless asked per connection, so we switch them on;
    otherwise a fact row could reference a security that does not exist.
    """
    url = url or get_settings().db_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_fk)
    return engine


def alembic_config(url: str) -> Config:
    """Alembic config pointing at the absolute migrations dir (works from any cwd)."""
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(engine_or_url: Engine | str | None = None, revision: str = "head") -> None:
    """Apply migrations up to `revision` (default head)."""
    if isinstance(engine_or_url, Engine):
        url = engine_or_url.url.render_as_string(hide_password=False)
    else:
        url = engine_or_url or get_settings().db_url
    command.upgrade(alembic_config(url), revision)


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> int:
    """Insert `rows`, resolving natural-key conflicts with an update (or do-nothing).

    Args:
        conn: Open connection (inside a transaction).
        table: Target table.
        rows: Row dicts; all rows should have the same keys.
        key_cols: Columns of the unique constraint that defines the natural key.
        update_cols: Columns to overwrite on conflict. None means every non-key column present
            in the rows; an empty sequence means DO NOTHING.

    Returns:
        Number of rows submitted.
    """
    if not rows:
        return 0
    dialect = conn.dialect.name
    if dialect == "sqlite":
        insert_fn: Any = sqlite.insert
    elif dialect == "postgresql":
        insert_fn = postgresql.insert
    else:  # pragma: no cover - only two supported backends
        raise NotImplementedError(f"upsert not supported for {dialect}")
    # Postgres refuses to touch the same row twice in one statement: keep the last duplicate.
    deduped: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        deduped[tuple(row[k] for k in key_cols)] = row
    rows = list(deduped.values())
    if update_cols is None:
        update_cols = [c for c in rows[0] if c not in key_cols]
    for i in range(0, len(rows), BATCH_SIZE):
        batch = list(rows[i : i + BATCH_SIZE])
        stmt = insert_fn(table).values(batch)
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(key_cols),
                set_={c: stmt.excluded[c] for c in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key_cols))
        conn.execute(stmt)
    return len(rows)
