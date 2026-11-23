"""Create account-scoped Drive knowledge tables and enable pgvector.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

LINK = ("user_id", "google_sub", "link_version")
FILE = (*LINK, "file_id")


class Vector1536(sa.types.UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kwargs):
        return "VECTOR(1536)"


def _link_columns():
    return (
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("link_version", sa.Integer(), nullable=False),
    )


def _file_columns():
    return (
        *_link_columns(),
        sa.Column("file_id", sa.String(255), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("drive_id", sa.String(255)),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("modified_time", sa.DateTime(timezone=True)),
    )


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    is_postgres = op.get_context().dialect.name == "postgresql"
    if is_postgres:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    if "drive_file_metadata" not in tables:
        op.create_table(
            "drive_file_metadata",
            *_file_columns(),
            sa.Column("indexed_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("status_reason", sa.Text()),
            sa.Column("embedding_model", sa.String(128)),
            sa.Column("parser_version", sa.String(64)),
            sa.PrimaryKeyConstraint(*FILE),
        )
        op.create_index("ix_drive_metadata_link_modified", "drive_file_metadata", [*LINK, "modified_time"])
        op.create_index("ix_drive_metadata_link_scope", "drive_file_metadata", [*LINK, "drive_id"])
    if "drive_scan_candidates" not in tables:
        op.create_table(
            "drive_scan_candidates",
            *_file_columns(),
            sa.PrimaryKeyConstraint(*FILE),
        )
        op.create_index("ix_drive_scan_link_modified", "drive_scan_candidates", [*LINK, "modified_time"])
    if "drive_file_chunks" not in tables:
        op.create_table(
            "drive_file_chunks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            *_link_columns(),
            sa.Column("file_id", sa.String(255), nullable=False),
            sa.Column("file_version", sa.String(64), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("page_or_section", sa.String(128)),
            sa.Column("encrypted_content", sa.Text(), nullable=False),
            sa.Column("tsv", TSVECTOR() if is_postgres else sa.Text(), nullable=False),
            sa.Column("embedding", Vector1536() if is_postgres else sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(FILE, [f"drive_file_metadata.{column}" for column in FILE], ondelete="CASCADE"),
            sa.UniqueConstraint(*FILE, "chunk_index", name="uq_drive_file_chunk"),
            sqlite_autoincrement=True,
        )
        if is_postgres:
            op.create_index("ix_drive_chunks_tsv", "drive_file_chunks", ["tsv"], postgresql_using="gin")
    if "drive_sync_checkpoints" not in tables:
        op.create_table(
            "drive_sync_checkpoints",
            *_link_columns(),
            sa.Column("scope", sa.String(255), nullable=False),
            sa.Column("scan_id", sa.String(36)),
            sa.Column("scan_page_token", sa.Text()),
            sa.Column("start_page_token", sa.Text()),
            sa.Column("scan_state", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("last_error_code", sa.String(64)),
            sa.Column("last_full_scan_at", sa.DateTime(timezone=True)),
            sa.Column("last_sync_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint(*LINK, "scope"),
        )
    if "drive_permission_states" not in tables:
        op.create_table(
            "drive_permission_states",
            *_link_columns(),
            sa.Column("file_id", sa.String(255), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checked_at", sa.DateTime(timezone=True)),
            sa.Column("file_version", sa.String(64)),
            sa.Column("scope", sa.String(255)),
            sa.PrimaryKeyConstraint(*FILE),
        )
        op.create_index("ix_drive_permissions_link_scope", "drive_permission_states", [*LINK, "scope"])


def downgrade():
    op.drop_table("drive_permission_states")
    op.drop_table("drive_sync_checkpoints")
    op.drop_table("drive_file_chunks")
    op.drop_table("drive_scan_candidates")
    op.drop_table("drive_file_metadata")
