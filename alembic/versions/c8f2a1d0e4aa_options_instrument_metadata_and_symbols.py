"""options instrument_metadata and wider symbol columns

Revision ID: c8f2a1d0e4aa
Revises: 7d9180214bee
Create Date: 2026-04-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8f2a1d0e4aa"
down_revision: Union[str, None] = "7d9180214bee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "signals",
        "symbol",
        existing_type=sa.String(length=20),
        type_=sa.String(length=72),
        existing_nullable=False,
    )
    op.alter_column(
        "orders",
        "symbol",
        existing_type=sa.String(length=20),
        type_=sa.String(length=72),
        existing_nullable=False,
    )
    op.add_column("orders", sa.Column("instrument_metadata", sa.JSON(), nullable=True))
    op.alter_column(
        "positions",
        "symbol",
        existing_type=sa.String(length=20),
        type_=sa.String(length=72),
        existing_nullable=False,
    )
    op.add_column("positions", sa.Column("instrument_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "instrument_metadata")
    op.alter_column(
        "positions",
        "symbol",
        existing_type=sa.String(length=72),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.drop_column("orders", "instrument_metadata")
    op.alter_column(
        "orders",
        "symbol",
        existing_type=sa.String(length=72),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.alter_column(
        "signals",
        "symbol",
        existing_type=sa.String(length=72),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
