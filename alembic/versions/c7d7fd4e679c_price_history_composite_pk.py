"""price_history_composite_pk

Revision ID: c7d7fd4e679c
Revises: d130a1b2c3d4
Create Date: 2026-05-26 14:31:58.453409

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'c7d7fd4e679c'
down_revision: Union[str, None] = 'd130a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _price_history_columns(bind) -> set:
    insp = inspect(bind)
    if not insp.has_table("price_history"):
        return set()
    return {c["name"] for c in insp.get_columns("price_history")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _price_history_columns(bind)
    if not columns:
        return

    if "id" in columns:
        # 1. Deduplicate rows to prevent primary key constraint violation
        op.execute(
            "DELETE FROM price_history a USING price_history b "
            "WHERE a.ctid < b.ctid "
            "AND a.timestamp = b.timestamp "
            "AND a.symbol = b.symbol "
            "AND a.timeframe = b.timeframe "
            "AND a.broker = b.broker;"
        )

        # 2. Drop the existing single-column primary key constraint
        try:
            op.drop_constraint("price_history_pkey", "price_history", type_="primary")
        except Exception:
            pass

        # 3. Drop the id column
        op.drop_column("price_history", "id")

        # 4. Create the new composite primary key
        op.create_primary_key(
            "price_history_pkey",
            "price_history",
            ["timestamp", "symbol", "timeframe", "broker"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = _price_history_columns(bind)
    if not columns:
        return

    if "id" not in columns:
        # 1. Drop the composite primary key constraint
        try:
            op.drop_constraint("price_history_pkey", "price_history", type_="primary")
        except Exception:
            pass

        # 2. Add back the id column as nullable initially
        op.add_column("price_history", sa.Column("id", sa.Integer(), nullable=True))

        # 3. Populate id using a sequence
        op.execute("CREATE SEQUENCE IF NOT EXISTS price_history_id_seq")
        op.execute("UPDATE price_history SET id = nextval('price_history_id_seq') WHERE id IS NULL")

        # 4. Make id non-nullable and set it as the primary key
        op.alter_column("price_history", "id", nullable=False)
        op.create_primary_key("price_history_pkey", "price_history", ["id"])
