import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models import Base


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


def init_db(db_url: str) -> tuple[Engine, sessionmaker]:
    engine = create_sqlite_engine(db_url)
    Base.metadata.create_all(bind=engine)
    # Migration check for TelegramUpdate
    from sqlalchemy import text

    with engine.connect() as conn:
        try:
            res = conn.execute(text("PRAGMA table_info(telegram_updates)"))
            cols = [r[1] for r in res.fetchall()]
            if cols:
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
                        text("ALTER TABLE telegram_updates ADD COLUMN lease_expires_at DATETIME")
                    )
                if "retry_at" not in cols:
                    conn.execute(text("ALTER TABLE telegram_updates ADD COLUMN retry_at DATETIME"))
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
                        text("ALTER TABLE telegram_updates ADD COLUMN updated_at DATETIME")
                    )
            conn.commit()
        except Exception:
            pass

        try:
            import secrets
            res = conn.execute(text("PRAGMA table_info(telegram_connections)"))
            cols = [r[1] for r in res.fetchall()]
            if cols:
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
            res_op = conn.execute(text("PRAGMA table_info(action_operations)"))
            op_cols = [r[1] for r in res_op.fetchall()]
            if op_cols and "telegram_binding_id" not in op_cols:
                conn.execute(text("ALTER TABLE action_operations ADD COLUMN telegram_binding_id VARCHAR(64)"))
                conn.commit()
        except Exception:
            pass

    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, session_factory


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)
