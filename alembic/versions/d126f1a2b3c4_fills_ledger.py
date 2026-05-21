"""fills_ledger

D126 — clean append-only fills ledger. One row per confirmed fill;
authoritative analytics ledger + race-free position-quantity source of
truth (position qty = SUM(signed_quantity)). Idempotent for existing DBs.

Revision ID: d126f1a2b3c4
Revises: d116a1b2c3d4
Create Date: 2026-05-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d126f1a2b3c4"
down_revision: Union[str, None] = "d116a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("fills"):
        return

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=72), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("signed_quantity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("fill_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("notional", sa.Numeric(precision=20, scale=8), nullable=False, server_default="0"),
        sa.Column("fee", sa.Numeric(precision=20, scale=8), nullable=False, server_default="0"),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("realised_pnl", sa.Numeric(precision=20, scale=8), nullable=False, server_default="0"),
        sa.Column("avg_cost_basis", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("position_qty_after", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("holding_period_sec", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("strategy", sa.String(length=64), nullable=True),
        sa.Column("signal_id", sa.String(), nullable=True),
        sa.Column("signal_confidence", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("run_session_id", sa.String(length=40), nullable=True),
        sa.Column("derisk_source", sa.String(length=32), nullable=True),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("broker_order_id", sa.String(), nullable=True),
        sa.Column("instrument_metadata", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fills_timestamp", "fills", ["timestamp"], unique=False)
    op.create_index("ix_fills_broker", "fills", ["broker"], unique=False)
    op.create_index("ix_fills_symbol", "fills", ["symbol"], unique=False)
    op.create_index("ix_fills_strategy", "fills", ["strategy"], unique=False)
    op.create_index("ix_fills_signal_id", "fills", ["signal_id"], unique=False)
    op.create_index("ix_fills_run_session_id", "fills", ["run_session_id"], unique=False)
    op.create_index("ix_fills_order_id", "fills", ["order_id"], unique=False)
    op.create_index("ix_fills_broker_symbol_ts", "fills", ["broker", "symbol", "timestamp"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("fills"):
        op.drop_table("fills")
