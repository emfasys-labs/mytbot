"""fill opening_strategy / opening_signal_id columns

D231 (P1.5) — loss-attribution review fix. ``fills.strategy`` on a CLOSING
fill names the exit mechanism (stop_loss_monitor / capital_recycle /
portfolio_orchestrator / profit_harvest_monitor), not the strategy that
opened the lot being closed, so per-entry-strategy round-trip expectancy
could not be computed from the ledger. Adds ``opening_strategy`` and
``opening_signal_id``, stamped on every fill with the strategy/signal that
started the position's current open streak. Both columns are nullable and
NOT backfilled onto existing rows (the true origin fill for an
already-closed pre-migration streak cannot be reconstructed retroactively
without ambiguity). Idempotent for existing DBs.

Revision ID: d231a1b2c3d4
Revises: c7d7fd4e679c
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d231a1b2c3d4"
down_revision: Union[str, None] = "c7d7fd4e679c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fills_columns(bind) -> set:
    insp = inspect(bind)
    if not insp.has_table("fills"):
        return set()
    return {c["name"] for c in insp.get_columns("fills")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _fills_columns(bind)
    if not cols:
        return  # fresh DB — create_all builds the full schema
    if "opening_strategy" not in cols:
        op.add_column("fills", sa.Column("opening_strategy", sa.String(64), nullable=True))
        op.create_index(
            "ix_fills_opening_strategy", "fills", ["opening_strategy"], unique=False
        )
    if "opening_signal_id" not in cols:
        op.add_column("fills", sa.Column("opening_signal_id", sa.String(), nullable=True))
        op.create_index(
            "ix_fills_opening_signal_id", "fills", ["opening_signal_id"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _fills_columns(bind)
    if "opening_signal_id" in cols:
        op.drop_index("ix_fills_opening_signal_id", table_name="fills")
        op.drop_column("fills", "opening_signal_id")
    if "opening_strategy" in cols:
        op.drop_index("ix_fills_opening_strategy", table_name="fills")
        op.drop_column("fills", "opening_strategy")
