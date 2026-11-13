import pytest
from sqlalchemy import inspect, text

from app.db import create_app_engine, init_db


def test_sqlite_engine_creation():
    engine = create_app_engine("sqlite:///:memory:")
    assert engine.dialect.name == "sqlite"
    
    with engine.connect() as conn:
        res = conn.execute(text("SELECT 1")).scalar()
        assert res == 1


def test_postgres_url_normalization():
    # Verify URL starting with postgresql:// is normalized to postgresql+psycopg://
    from app.db import normalize_db_url
    
    raw_url = "postgresql://user:pass@localhost:5432/testdb"
    normalized = normalize_db_url(raw_url)
    assert normalized == "postgresql+psycopg://user:pass@localhost:5432/testdb"
    
    sqlite_url = "sqlite:///gateway.db"
    assert normalize_db_url(sqlite_url) == "sqlite:///gateway.db"


def test_init_db_portable_inspector():
    engine, session_factory = init_db("sqlite:///:memory:")
    assert engine is not None
    assert session_factory is not None
    
    # Check that inspect works without SQLite PRAGMA dependencies
    with engine.connect() as conn:
        insp = inspect(conn)
        tables = insp.get_table_names()
        assert "users" in tables
        assert "telegram_updates" in tables
        assert "action_operations" in tables
        
        cols = [c["name"] for c in insp.get_columns("telegram_updates")]
        assert "delivery_status" in cols
        assert "binding_id" in [c["name"] for c in insp.get_columns("telegram_connections")]

