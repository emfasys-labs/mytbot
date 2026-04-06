"""parameter_log_table

Add parameter_log audit table for ParameterManager overrides.
Idempotent for existing databases.

Revision ID: e1c3a9f41b77
Revises: d4e8f1a20002
Create Date: 2026-04-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "e1c3a9f41b77"
down_revision: Union[str, None] = "d4e8f1a20002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("parameter_log"):
        return

    op.create_table(
        "parameter_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("parameter", sa.String(length=64), nullable=False),
        sa.Column("layer", sa.String(length=20), nullable=False),
        sa.Column("old_value", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("new_value", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="system"),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("expiry_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parameter_log_timestamp", "parameter_log", ["timestamp"], unique=False)
    op.create_index("ix_parameter_log_parameter", "parameter_log", ["parameter"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("parameter_log"):
        op.drop_table("parameter_log")
