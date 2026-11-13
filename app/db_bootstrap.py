"""Initialize a new PostgreSQL database or upgrade a versioned database."""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.business import models as business_models  # noqa: F401
from app.config import Settings
from app.db import create_app_engine
from app.models import Base
from app.quotations import execution, jobs  # noqa: F401
from app.reporting import models as reporting_models  # noqa: F401


def main():
    engine = create_app_engine(Settings().DATABASE_URL)
    config = Config("alembic.ini")
    try:
        tables = set(inspect(engine).get_table_names())
        if not tables:
            Base.metadata.create_all(engine)
            command.stamp(config, "head")
        elif "alembic_version" in tables:
            command.upgrade(config, "head")
        else:
            raise RuntimeError("Existing database has no Alembic version. Verify its schema before adoption.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
