"""Add link_version to google_connections to separate stable index link identity from token refresh concurrency.

Revision ID: d1e2f3a4b5c6
Revises: c7d8e9f0a1b2
Create Date: 2026-11-16 14:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "google_connections" in tables:
        cols = {c["name"] for c in insp.get_columns("google_connections")}
        if "link_version" not in cols:
            with op.batch_alter_table("google_connections") as table:
                table.add_column(
                    sa.Column("link_version", sa.Integer(), nullable=False, server_default="1")
                )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "google_connections" in tables:
        cols = {c["name"] for c in insp.get_columns("google_connections")}
        if "link_version" in cols:
            with op.batch_alter_table("google_connections") as table:
                table.drop_column("link_version")
