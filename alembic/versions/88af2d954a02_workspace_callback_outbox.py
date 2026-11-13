"""Persist Workspace callback delivery state and support large Telegram IDs."""

from alembic import op
import sqlalchemy as sa

revision = "88af2d954a02"
down_revision = "35696dbcf459"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workspace_jobs") as table:
        table.drop_index("ix_workspace_jobs_user_request")
        table.create_index("ix_workspace_jobs_user_request", ["user_id", "request_id"], unique=True)
        table.add_column(sa.Column("callback_delivered_at", sa.DateTime(), nullable=True))
        table.add_column(sa.Column("callback_retry_at", sa.DateTime(), nullable=True))
        table.create_index("ix_workspace_jobs_callback_retry_at", ["callback_retry_at"])
        for name in ("telegram_chat_id", "telegram_user_id"):
            table.alter_column(name, existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade():
    with op.batch_alter_table("workspace_jobs") as table:
        table.drop_index("ix_workspace_jobs_user_request")
        table.create_index("ix_workspace_jobs_user_request", ["user_id", "request_id"])
        table.drop_index("ix_workspace_jobs_callback_retry_at")
        table.drop_column("callback_retry_at")
        table.drop_column("callback_delivered_at")
