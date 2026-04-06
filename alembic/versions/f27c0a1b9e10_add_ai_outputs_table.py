"""add ai outputs table

Revision ID: f27c0a1b9e10
Revises: e1c3a9f41b77
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f27c0a1b9e10"
down_revision = "e1c3a9f41b77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("ai_outputs"):
        op.create_table(
            "ai_outputs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("context_type", sa.String(length=20), nullable=False),
            sa.Column("score", sa.Numeric(precision=10, scale=6), nullable=True),
            sa.Column("confidence", sa.Numeric(precision=10, scale=6), nullable=True),
            sa.Column("event_type", sa.String(length=32), nullable=True),
            sa.Column("regime_label", sa.String(length=64), nullable=True),
            sa.Column("decay_hours", sa.Integer(), nullable=True),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("signal_id", sa.String(length=128), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {ix.get("name") for ix in inspector.get_indexes("ai_outputs")}
    if "ix_ai_outputs_timestamp" not in existing_indexes:
        op.create_index("ix_ai_outputs_timestamp", "ai_outputs", ["timestamp"], unique=False)
    if "ix_ai_outputs_symbol" not in existing_indexes:
        op.create_index("ix_ai_outputs_symbol", "ai_outputs", ["symbol"], unique=False)
    if "ix_ai_outputs_context_type" not in existing_indexes:
        op.create_index("ix_ai_outputs_context_type", "ai_outputs", ["context_type"], unique=False)
    if "ix_ai_outputs_signal_id" not in existing_indexes:
        op.create_index("ix_ai_outputs_signal_id", "ai_outputs", ["signal_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ai_outputs"):
        return

    existing_indexes = {ix.get("name") for ix in inspector.get_indexes("ai_outputs")}
    if "ix_ai_outputs_signal_id" in existing_indexes:
        op.drop_index("ix_ai_outputs_signal_id", table_name="ai_outputs")
    if "ix_ai_outputs_context_type" in existing_indexes:
        op.drop_index("ix_ai_outputs_context_type", table_name="ai_outputs")
    if "ix_ai_outputs_symbol" in existing_indexes:
        op.drop_index("ix_ai_outputs_symbol", table_name="ai_outputs")
    if "ix_ai_outputs_timestamp" in existing_indexes:
        op.drop_index("ix_ai_outputs_timestamp", table_name="ai_outputs")
    op.drop_table("ai_outputs")
