from pathlib import Path

from findata.settings import Settings


def test_default_db_url_is_sqlite_under_data_dir(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path, database_url="")
    assert s.db_url.startswith("sqlite:///")
    assert s.db_url.endswith("findata.db")


def test_explicit_database_url_wins(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path, database_url="postgresql+psycopg://u:p@h/db")
    assert s.db_url == "postgresql+psycopg://u:p@h/db"


def test_user_agent_includes_contact() -> None:
    assert "x@y.z" in Settings(contact_email="x@y.z").user_agent
