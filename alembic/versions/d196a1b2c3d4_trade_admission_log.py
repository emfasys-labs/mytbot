"""trade_admission_log

D196 — Trade Admission Intelligence ledger. One row per executable candidate
reaching the shared pre-risk chokepoint: the admission decision, the features
it saw, downstream status, and later multi-horizon outcome snapshots / rich
myTbot-native labels. Idempotent for existing DBs (no-op if the table exists).

Revision ID: d196a1b2c3d4
Revises: 9c1f0b7c4a11
Create Date: 2026-06-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d196a1b2c3d4"
down_revision: Union[str, None] = "9c1f0b7c4a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if insp.has_table("trade_admission_log"):
        # Table already exists (created by create_all). Add any columns that a
        # pre-multi-horizon build may be missing.
        existing = {c["name"] for c in insp.get_columns("trade_admission_log")}
        for col in (
            sa.Column("outcome_horizons", sa.JSON(), nullable=True),
            sa.Column("outcome_labels", sa.JSON(), nullable=True),
        ):
            if col.name not in existing:
                op.add_column("trade_admission_log", col)
        return

    op.create_table(
        "trade_admission_log",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loop_iteration", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=72), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("broker", sa.String(length=20), nullable=True),
        sa.Column("asset_class", sa.String(length=20), nullable=True),
        sa.Column("signal_id", sa.String(length=128), nullable=True),
        sa.Column("source_path", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("shadow_only", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("active_applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("admission_score", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("uncertainty", sa.Numeric(precision=12, scale=8), nullable=True),
        sa.Column("suggested_notional", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("suggested_quantity", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("suggested_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("downstream_status", sa.String(length=40), nullable=True),
        sa.Column("downstream_reason", sa.Text(), nullable=True),
        sa.Column("execution_status", sa.String(length=40), nullable=True),
        sa.Column("outcome_label", sa.String(length=40), nullable=True),
        sa.Column("outcome_net_pnl", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("outcome_return", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("outcome_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_horizons", sa.JSON(), nullable=True),
        sa.Column("outcome_labels", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_admission_log_timestamp", "trade_admission_log", ["timestamp"])
    op.create_index("ix_trade_admission_log_loop_iteration", "trade_admission_log", ["loop_iteration"])
    op.create_index("ix_trade_admission_log_symbol", "trade_admission_log", ["symbol"])
    op.create_index("ix_trade_admission_log_strategy", "trade_admission_log", ["strategy"])
    op.create_index("ix_trade_admission_log_broker", "trade_admission_log", ["broker"])
    op.create_index("ix_trade_admission_log_asset_class", "trade_admission_log", ["asset_class"])
    op.create_index("ix_trade_admission_log_signal_id", "trade_admission_log", ["signal_id"])
    op.create_index("ix_trade_admission_log_decision", "trade_admission_log", ["decision"])
    op.create_index("ix_trade_admission_log_downstream_status", "trade_admission_log", ["downstream_status"])
    op.create_index("ix_trade_admission_log_outcome_label", "trade_admission_log", ["outcome_label"])
    op.create_index("ix_trade_admission_symbol_ts", "trade_admission_log", ["symbol", "timestamp"])
    op.create_index("ix_trade_admission_signal_id", "trade_admission_log", ["signal_id"])
    op.create_index("ix_trade_admission_decision_ts", "trade_admission_log", ["decision", "timestamp"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("trade_admission_log"):
        op.drop_table("trade_admission_log")
