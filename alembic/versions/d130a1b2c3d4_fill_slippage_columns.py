"""fill slippage columns

D130 — per-fill execution-quality capture. Adds ``intended_price`` and
``slippage_bps`` to the ``fills`` ledger so execution drag (the gap
between the signal's target price and the actual fill) becomes a
first-class, measurable quantity. Both columns are nullable: they cannot
be backfilled onto pre-D130 fills and are only forward-captured.
Idempotent for existing DBs.

Revision ID: d130a1b2c3d4
Revises: d127a1b2c3d4
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d130a1b2c3d4"
down_revision: Union[str, None] = "d127a1b2c3d4"
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
    if "intended_price" not in cols:
        op.add_column("fills", sa.Column("intended_price", sa.Numeric(20, 8), nullable=True))
    if "slippage_bps" not in cols:
        op.add_column("fills", sa.Column("slippage_bps", sa.Numeric(12, 4), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _fills_columns(bind)
    if "slippage_bps" in cols:
        op.drop_column("fills", "slippage_bps")
    if "intended_price" in cols:
        op.drop_column("fills", "intended_price")
