"""Persist Telegram PDF controls and approval deliveries on versioned databases."""

from alembic import op
import sqlalchemy as sa

revision = "c41d2e8a9b70"
down_revision = "88af2d954a02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quote_pdf_messages",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant", sa.String(500), nullable=False),
        sa.Column("database", sa.String(250), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "quote_approval_deliveries",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("pdf_token", sa.String(32), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("approver_user_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("feedback_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table("quote_approval_deliveries")
    op.drop_table("quote_pdf_messages")
