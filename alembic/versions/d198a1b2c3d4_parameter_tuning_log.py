"""parameter_tuning_log

Adaptive Tuner audit ledger — one row per applied bounded parameter change.
Idempotent for existing DBs (no-op if the table already exists).

Revision ID: d198a1b2c3d4
Revises: d196a1b2c3d4
Create Date: 2026-06-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d198a1b2c3d4"
down_revision: Union[str, None] = "d196a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("parameter_tuning_log"):
        return
    op.create_table(
        "parameter_tuning_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameter", sa.String(length=96), nullable=False),
        sa.Column("regime", sa.String(length=32), nullable=True),
        sa.Column("old_value", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("new_value", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=True),
        sa.Column("reward", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parameter_tuning_log_timestamp", "parameter_tuning_log", ["timestamp"])
    op.create_index("ix_parameter_tuning_log_parameter", "parameter_tuning_log", ["parameter"])
    op.create_index("ix_parameter_tuning_log_regime", "parameter_tuning_log", ["regime"])
    op.create_index("ix_parameter_tuning_param_ts", "parameter_tuning_log", ["parameter", "timestamp"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("parameter_tuning_log"):
        op.drop_table("parameter_tuning_log")
