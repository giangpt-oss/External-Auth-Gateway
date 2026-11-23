"""Add connection_version to google_connections and create workspace coordination tables.

Revision ID: c7d8e9f0a1b2
Revises: 88af2d954a02
Create Date: 2026-11-16 11:05:00
"""

from alembic import op
import sqlalchemy as sa

revision = "c7d8e9f0a1b2"
down_revision = "88af2d954a02"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    # 1. Add connection_version to google_connections if not present
    if "google_connections" in tables:
        cols = {c["name"] for c in insp.get_columns("google_connections")}
        if "connection_version" not in cols:
            with op.batch_alter_table("google_connections") as table:
                table.add_column(
                    sa.Column("connection_version", sa.Integer(), nullable=False, server_default="1")
                )

    # 2. Add action and operation_id to workspace_jobs if not present
    if "workspace_jobs" in tables:
        job_cols = {c["name"] for c in insp.get_columns("workspace_jobs")}
        indexes = {idx["name"] for idx in insp.get_indexes("workspace_jobs")}
        with op.batch_alter_table("workspace_jobs") as table:
            if "action" not in job_cols:
                table.add_column(
                    sa.Column("action", sa.String(length=32), nullable=False, server_default="read")
                )
            if "operation_id" not in job_cols:
                table.add_column(
                    sa.Column("operation_id", sa.String(length=64), nullable=True)
                )
            if "ix_workspace_jobs_operation_id" not in indexes and "operation_id" not in job_cols:
                table.create_index("ix_workspace_jobs_operation_id", ["operation_id"])

    # 3. Create oauth_refresh_leases table if not present
    if "oauth_refresh_leases" not in tables:
        op.create_table(
            "oauth_refresh_leases",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("lease_holder", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("connection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
        )
        op.create_index("ix_oauth_refresh_leases_user_id", "oauth_refresh_leases", ["user_id"])
        op.create_index("ix_oauth_refresh_leases_lease_expires_at", "oauth_refresh_leases", ["lease_expires_at"])

    # 4. Create workspace_idempotency_records table if not present
    if "workspace_idempotency_records" not in tables:
        op.create_table(
            "workspace_idempotency_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("operation_id", sa.String(length=64), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="in_progress"),
            sa.Column("resource_id", sa.String(length=128), nullable=True),
            sa.Column("response_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("operation_id"),
        )
        op.create_index("ix_workspace_idempotency_records_user_id", "workspace_idempotency_records", ["user_id"])
        op.create_index("ix_workspace_idempotency_records_operation_id", "workspace_idempotency_records", ["operation_id"])
        op.create_index("ix_workspace_idempotency_records_payload_hash", "workspace_idempotency_records", ["payload_hash"])
        op.create_index("ix_workspace_idempotency_lookup", "workspace_idempotency_records", ["user_id", "operation_id"])


def downgrade():
    # The other tables and columns predate this revision on Stage 2 installations.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "google_connections" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("google_connections")}
        if "connection_version" in cols:
            with op.batch_alter_table("google_connections") as table:
                table.drop_column("connection_version")
