"""Test upgrading PostgreSQL / SQLite schema from revision 88af2d954a02 to head.
Proves that google_connections.connection_version is successfully added,
workspace coordination tables are created, and downgrade returns cleanly to 88af2d954a02.
"""

import os
import tempfile
import pytest
from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def alembic_test_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_url = f"sqlite:///{path}"
    engine = create_engine(db_url)

    # Recreate the pre-existing state at revision 88af2d954a02:
    # Core tables (users, google_connections without connection_version, workspace_jobs)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(255) NOT NULL,
                active BOOLEAN DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE google_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                google_sub VARCHAR(255) NOT NULL UNIQUE,
                email VARCHAR(255) NOT NULL,
                scopes TEXT NOT NULL,
                encrypted_access_token TEXT NOT NULL,
                encrypted_refresh_token TEXT,
                expires_at TIMESTAMP NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'connected',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """))
        conn.execute(text("""
            CREATE TABLE workspace_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id VARCHAR(64) NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                request_id VARCHAR(128) NOT NULL,
                service VARCHAR(32) NOT NULL,
                operation VARCHAR(128) NOT NULL,
                protocol VARCHAR(16) NOT NULL DEFAULT 'auto',
                is_write BOOLEAN NOT NULL DEFAULT 0,
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                phase VARCHAR(64),
                payload_encrypted TEXT NOT NULL,
                result_encrypted TEXT,
                error_code VARCHAR(64),
                error_message TEXT,
                worker_id VARCHAR(64),
                lease_expires_at TIMESTAMP,
                retry_count INTEGER NOT NULL DEFAULT 0,
                deadline_at TIMESTAMP NOT NULL,
                telegram_chat_id BIGINT,
                telegram_user_id BIGINT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                callback_delivered_at TIMESTAMP,
                callback_retry_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX ix_workspace_jobs_user_request ON workspace_jobs (user_id, request_id)"))
        conn.execute(text("INSERT INTO users (id, email) VALUES (2, 'legacy@example.invalid')"))
        conn.execute(text("""
            INSERT INTO google_connections (
                id, user_id, google_sub, email, scopes, encrypted_access_token, expires_at
            ) VALUES (
                17, 2, 'legacy-sub', 'legacy@example.invalid', 'openid', 'legacy-encrypted', CURRENT_TIMESTAMP
            )
        """))

    # Configure Alembic
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Stamp the database as having revision 88af2d954a02
    command.stamp(cfg, "88af2d954a02")

    yield cfg, engine, db_url

    engine.dispose()
    import gc
    gc.collect()
    try:
        if os.path.exists(path):
            os.remove(path)
    except PermissionError:
        pass


def test_upgrade_from_88af2d954a02_adds_connection_version_and_tables(alembic_test_db):
    cfg, engine, db_url = alembic_test_db

    # 1. Verify that before upgrade, google_connections does NOT have connection_version
    insp = inspect(engine)
    cols_before = {c["name"] for c in insp.get_columns("google_connections")}
    assert "connection_version" not in cols_before, "connection_version should NOT exist at 88af2d954a02"
    assert "oauth_refresh_leases" not in insp.get_table_names()
    assert "workspace_idempotency_records" not in insp.get_table_names()

    # 2. Upgrade all revisions and verify the existing account was selected.
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        migrated = conn.execute(text("""
            SELECT s.active_account_id, g.encrypted_access_token
            FROM google_account_selections s
            JOIN google_connections g ON g.id = s.active_account_id
            WHERE s.user_id = 2
        """)).one()
        assert migrated == (17, "legacy-encrypted")

    # 3. Verify that connection_version is now present and tables are created
    insp = inspect(engine)
    cols_after = {c["name"] for c in insp.get_columns("google_connections")}
    assert "connection_version" in cols_after, "connection_version MUST exist after upgrade"

    tables_after = set(insp.get_table_names())
    assert "oauth_refresh_leases" in tables_after, "oauth_refresh_leases must exist after upgrade"
    assert "workspace_idempotency_records" in tables_after, "workspace_idempotency_records must exist after upgrade"

    # Verify workspace_jobs has action and operation_id
    job_cols_after = {c["name"] for c in insp.get_columns("workspace_jobs")}
    assert "action" in job_cols_after
    assert "operation_id" in job_cols_after

    # 4. Insert and query a connection record to prove default value works
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (1, 'test@example.com')"))
        conn.execute(text("""
            INSERT INTO google_connections (
                user_id, google_sub, email, scopes, encrypted_access_token, expires_at
            ) VALUES (
                1, 'sub_123', 'test@example.com', 'openid', 'enc_token', CURRENT_TIMESTAMP
            )
        """))
        res = conn.execute(text("SELECT connection_version FROM google_connections WHERE user_id = 1")).scalar()
        assert res == 1

    # 5. Run alembic downgrade back to 88af2d954a02
    command.downgrade(cfg, "88af2d954a02")

    # 6. Rollback removes only the Stage 3 column and preserves coordination state.
    insp = inspect(engine)
    cols_downgraded = {c["name"] for c in insp.get_columns("google_connections")}
    assert "connection_version" not in cols_downgraded
    tables_downgraded = set(insp.get_table_names())
    assert {"oauth_refresh_leases", "workspace_idempotency_records"} <= tables_downgraded
    assert {"action", "operation_id"} <= {c["name"] for c in insp.get_columns("workspace_jobs")}
