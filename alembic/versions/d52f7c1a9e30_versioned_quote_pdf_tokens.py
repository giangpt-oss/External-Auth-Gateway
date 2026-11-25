"""Persist versioned quotation PDF tokens from the remote review flow."""

from alembic import op
import sqlalchemy as sa

revision = "d52f7c1a9e30"
down_revision = "c41d2e8a9b70"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quotation_pdf_tokens",
        sa.Column("token", sa.String(24), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant", sa.String(500), nullable=False),
        sa.Column("database", sa.String(250), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_binding_id", sa.String(128), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("order_name", sa.String(128), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("order_version", sa.String(64), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("requester_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("requester_binding_id", sa.String(128), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_quotation_pdf_tokens_user_id", "quotation_pdf_tokens", ["user_id"])
    op.create_index("ix_quotation_pdf_tokens_order_id", "quotation_pdf_tokens", ["order_id"])


def downgrade():
    op.drop_index("ix_quotation_pdf_tokens_order_id", table_name="quotation_pdf_tokens")
    op.drop_index("ix_quotation_pdf_tokens_user_id", table_name="quotation_pdf_tokens")
    op.drop_table("quotation_pdf_tokens")
