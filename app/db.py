import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models import Base


def normalize_db_url(db_url: str) -> str:
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_url


def create_postgres_engine(db_url: str) -> Engine:
    normalized_url = normalize_db_url(db_url)
    return create_engine(
        normalized_url,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        pool_recycle=1800,
    )


def create_sqlite_engine(db_url: str) -> Engine:
    # Ensure directory exists for file-based sqlite
    if db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:///:memory:"):
        db_file = db_url.replace("sqlite:///", "")
        db_path = Path(db_file)
        if not db_path.is_absolute():
            db_path = Path(os.getcwd()) / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    return engine


def create_app_engine(db_url: str) -> Engine:
    normalized_url = normalize_db_url(db_url)
    if normalized_url.startswith("sqlite"):
        return create_sqlite_engine(normalized_url)
    return create_postgres_engine(normalized_url)


def init_db(db_url: str) -> tuple[Engine, sessionmaker]:
    engine = create_app_engine(db_url)
    Base.metadata.create_all(bind=engine)

    from sqlalchemy import inspect, text

    with engine.connect() as conn:
        try:
            insp = inspect(conn)
            table_names = set(insp.get_table_names())
            if "telegram_updates" in table_names:
                cols = {c["name"] for c in insp.get_columns("telegram_updates")}
                if "ai_response" not in cols:
                    conn.execute(text("ALTER TABLE telegram_updates ADD COLUMN ai_response TEXT"))
                if "delivery_status" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE telegram_updates ADD COLUMN delivery_status VARCHAR(32) DEFAULT 'none' NOT NULL"
                        )
                    )
                if "delivery_chunks" not in cols:
                    conn.execute(
                        text("ALTER TABLE telegram_updates ADD COLUMN delivery_chunks TEXT")
                    )
                if "delivery_index" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE telegram_updates ADD COLUMN delivery_index INTEGER DEFAULT 0 NOT NULL"
                        )
                    )
                if "lease_expires_at" not in cols:
                    conn.execute(
                        text("ALTER TABLE telegram_updates ADD COLUMN lease_expires_at TIMESTAMP")
                    )
                if "retry_at" not in cols:
                    conn.execute(text("ALTER TABLE telegram_updates ADD COLUMN retry_at TIMESTAMP"))
                if "last_error" not in cols:
                    conn.execute(text("ALTER TABLE telegram_updates ADD COLUMN last_error TEXT"))
                if "claim_token" not in cols:
                    conn.execute(
                        text("ALTER TABLE telegram_updates ADD COLUMN claim_token VARCHAR(64)")
                    )
                if "user_id" not in cols:
                    conn.execute(
                        text(
                            "ALTER TABLE telegram_updates ADD COLUMN user_id INTEGER REFERENCES users(id)"
                        )
                    )
                if "updated_at" not in cols:
                    conn.execute(
                        text("ALTER TABLE telegram_updates ADD COLUMN updated_at TIMESTAMP")
                    )
                conn.commit()
        except Exception:
            pass

        try:
            import secrets
            insp = inspect(conn)
            table_names = set(insp.get_table_names())
            if "telegram_connections" in table_names:
                cols = {c["name"] for c in insp.get_columns("telegram_connections")}
                if "binding_id" not in cols:
                    conn.execute(text("ALTER TABLE telegram_connections ADD COLUMN binding_id VARCHAR(64)"))
                res_null = conn.execute(text("SELECT id FROM telegram_connections WHERE binding_id IS NULL"))
                for row in res_null.fetchall():
                    new_bid = f"tg_bind_{secrets.token_hex(16)}"
                    conn.execute(
                        text("UPDATE telegram_connections SET binding_id = :bid WHERE id = :cid"),
                        {"bid": new_bid, "cid": row[0]},
                    )
                conn.commit()
        except Exception:
            pass

        try:
            insp = inspect(conn)
            table_names = set(insp.get_table_names())
            if "action_operations" in table_names:
                op_cols = {c["name"] for c in insp.get_columns("action_operations")}
                if "telegram_binding_id" not in op_cols:
                    conn.execute(text("ALTER TABLE action_operations ADD COLUMN telegram_binding_id VARCHAR(64)"))
                    conn.commit()
        except Exception:
            pass

    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("workspace_jobs")}
            for name in ("callback_retry_at", "callback_delivered_at"):
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE workspace_jobs ADD COLUMN {name} TIMESTAMP"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workspace_jobs_callback_retry_at "
                              "ON workspace_jobs (callback_retry_at)"))
            indexes = {index["name"]: index for index in inspect(conn).get_indexes("workspace_jobs")}
            request_index = indexes.get("ix_workspace_jobs_user_request")
            if request_index is not None and not request_index["unique"]:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_jobs_user_request "
                                  "ON workspace_jobs (user_id, request_id)"))

    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)
