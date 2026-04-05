"""initial_schema

Baseline schema aligned with ``storage.models`` (M1).

- **Empty database:** creates all tables via ``Base.metadata.create_all``.
- **Already populated** (e.g. tables from ``storage/db.py`` ``create_all``): no DDL; revision
  still applies so ``alembic upgrade head`` is safe.

Timescale hypertable for ``price_history`` remains optional (see ``storage/db._try_create_price_hypertable``).

Revision ID: b611a4f88c2b
Revises:
Create Date: 2026-04-05 21:24:54.306748

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

from storage.models import Base

# revision identifiers, used by Alembic.
revision: str = "b611a4f88c2b"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if inspect(conn).has_table("signals"):
        return
    Base.metadata.create_all(bind=conn)


def downgrade() -> None:
    conn = op.get_bind()
    Base.metadata.drop_all(bind=conn)
