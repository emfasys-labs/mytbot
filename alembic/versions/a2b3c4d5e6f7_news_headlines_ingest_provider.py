"""Add news_headlines.ingest_provider for per-feed dashboard age.

Revision ID: a2b3c4d5e6f7
Revises: c8f2a1d0e4aa
Create Date: 2026-04-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "c8f2a1d0e4aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("news_headlines", sa.Column("ingest_provider", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_news_headlines_ingest_provider",
        "news_headlines",
        ["ingest_provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_news_headlines_ingest_provider", table_name="news_headlines")
    op.drop_column("news_headlines", "ingest_provider")
