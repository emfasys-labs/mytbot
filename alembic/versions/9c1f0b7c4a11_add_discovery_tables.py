"""add discovery tables

Revision ID: 9c1f0b7c4a11
Revises: a1d9c3b2e770
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa


revision = "9c1f0b7c4a11"
down_revision = "a1d9c3b2e770"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("anomaly_log"):
        op.create_table(
            "anomaly_log",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("symbol", sa.String(length=20), nullable=False),
            sa.Column("asset_class", sa.String(length=20), nullable=False),
            sa.Column("direction", sa.String(length=8), nullable=False),
            sa.Column("price_move_pct", sa.Numeric(precision=10, scale=4), nullable=False),
            sa.Column("price_z_score", sa.Numeric(precision=10, scale=4), nullable=False),
            sa.Column("volume_z_score", sa.Numeric(precision=10, scale=4), nullable=True),
            sa.Column("news_velocity", sa.Numeric(precision=10, scale=4), nullable=True),
            sa.Column("news_sentiment", sa.Numeric(precision=10, scale=4), nullable=True),
            sa.Column("anomaly_score", sa.Numeric(precision=10, scale=4), nullable=False),
            sa.Column("opportunities_found", sa.Integer(), nullable=True),
            sa.Column("thesis_generated", sa.Boolean(), nullable=True),
            sa.Column("signals_produced", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    aidx = {ix.get("name") for ix in inspector.get_indexes("anomaly_log")}
    if "ix_anomaly_log_timestamp" not in aidx:
        op.create_index("ix_anomaly_log_timestamp", "anomaly_log", ["timestamp"], unique=False)
    if "ix_anomaly_log_symbol" not in aidx:
        op.create_index("ix_anomaly_log_symbol", "anomaly_log", ["symbol"], unique=False)

    if not inspector.has_table("thesis_log"):
        op.create_table(
            "thesis_log",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trigger_symbol", sa.String(length=20), nullable=False),
            sa.Column("trigger_direction", sa.String(length=8), nullable=False),
            sa.Column("trigger_explanation", sa.Text(), nullable=False),
            sa.Column("overall_confidence", sa.Numeric(precision=10, scale=4), nullable=False),
            sa.Column("time_horizon_hours", sa.Integer(), nullable=False),
            sa.Column("opportunities", sa.JSON(), nullable=True),
            sa.Column("invalidation_conditions", sa.JSON(), nullable=True),
            sa.Column("model_used", sa.String(length=64), nullable=False),
            sa.Column("tokens_used", sa.Integer(), nullable=True),
            sa.Column("ai_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    tidx = {ix.get("name") for ix in inspector.get_indexes("thesis_log")}
    if "ix_thesis_log_timestamp" not in tidx:
        op.create_index("ix_thesis_log_timestamp", "thesis_log", ["timestamp"], unique=False)
    if "ix_thesis_log_trigger_symbol" not in tidx:
        op.create_index("ix_thesis_log_trigger_symbol", "thesis_log", ["trigger_symbol"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("thesis_log"):
        tidx = {ix.get("name") for ix in inspector.get_indexes("thesis_log")}
        if "ix_thesis_log_trigger_symbol" in tidx:
            op.drop_index("ix_thesis_log_trigger_symbol", table_name="thesis_log")
        if "ix_thesis_log_timestamp" in tidx:
            op.drop_index("ix_thesis_log_timestamp", table_name="thesis_log")
        op.drop_table("thesis_log")

    if inspector.has_table("anomaly_log"):
        aidx = {ix.get("name") for ix in inspector.get_indexes("anomaly_log")}
        if "ix_anomaly_log_symbol" in aidx:
            op.drop_index("ix_anomaly_log_symbol", table_name="anomaly_log")
        if "ix_anomaly_log_timestamp" in aidx:
            op.drop_index("ix_anomaly_log_timestamp", table_name="anomaly_log")
        op.drop_table("anomaly_log")
