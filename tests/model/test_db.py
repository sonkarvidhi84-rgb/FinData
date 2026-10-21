from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    select,
    text,
)

from findata.model import tables as t
from findata.model.db import get_engine, upgrade, upsert


@pytest.fixture
def kv_table() -> tuple[MetaData, Table]:
    md = MetaData()
    tbl = Table(
        "kv",
        md,
        Column("id", Integer, primary_key=True),
        Column("k", String, nullable=False),
        Column("v", String),
        Column("w", String),
        UniqueConstraint("k"),
    )
    return md, tbl


def test_upsert_insert_update_and_do_nothing(kv_table: tuple[MetaData, Table]) -> None:
    md, tbl = kv_table
    engine = get_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        md.create_all(conn)
        assert upsert(conn, tbl, [{"k": "a", "v": "1", "w": "x"}], ["k"]) == 1
        upsert(conn, tbl, [{"k": "a", "v": "2", "w": "y"}], ["k"])  # update all non-key
        assert conn.execute(select(tbl.c.v, tbl.c.w)).one() == ("2", "y")
        upsert(conn, tbl, [{"k": "a", "v": "3", "w": "z"}], ["k"], update_cols=["v"])
        assert conn.execute(select(tbl.c.v, tbl.c.w)).one() == ("3", "y")
        upsert(conn, tbl, [{"k": "a", "v": "9", "w": "9"}], ["k"], update_cols=[])
        assert conn.execute(select(tbl.c.v)).scalar_one() == "3"
        assert upsert(conn, tbl, [], ["k"]) == 0


def test_upsert_dedupes_within_batch(kv_table: tuple[MetaData, Table]) -> None:
    md, tbl = kv_table
    engine = get_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        md.create_all(conn)
        rows = [{"k": "a", "v": "1", "w": None}, {"k": "a", "v": "2", "w": None}]
        upsert(conn, tbl, rows, ["k"])
        assert conn.execute(select(tbl.c.v)).all() == [("2",)]


def test_upgrade_creates_all_tables_and_view(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'sub' / 'w.db').as_posix()}"
    engine = get_engine(url)  # creates parent dir
    upgrade(engine)
    insp = inspect(engine)
    assert set(t.metadata.tables) <= set(insp.get_table_names())
    assert "v_price_adjusted" in insp.get_view_names()
    upgrade(url)  # idempotent at head
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM v_price_adjusted")).scalar_one() == 0
    engine.dispose()
