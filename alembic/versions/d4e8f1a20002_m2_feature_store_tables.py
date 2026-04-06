"""M2 feature store, news headlines, macro observations.

Adds tables for existing databases that already applied ``b611a4f88c2b`` before these
models existed. Idempotent: skips any table that already exists.

Revision ID: d4e8f1a20002
Revises: b611a4f88c2b
Create Date: 2026-04-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d4e8f1a20002"
down_revision: Union[str, None] = "b611a4f88c2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    if not inspect(bind).has_table("feature_snapshots"):
        op.create_table(
            "feature_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("timeframe", sa.String(length=8), nullable=False),
            sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("open", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("high", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("low", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("close", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("volume", sa.Numeric(precision=30, scale=8), nullable=False),
            sa.Column("features", sa.JSON(), nullable=False),
            sa.Column("validation", sa.JSON(), nullable=True),
            sa.Column("data_source", sa.String(length=20), nullable=False, server_default="yfinance"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "symbol",
                "timeframe",
                "bar_timestamp",
                name="uq_feature_snapshots_symbol_tf_bar_ts",
            ),
        )
        op.create_index(
            "ix_feature_symbol_tf_ts",
            "feature_snapshots",
            ["symbol", "timeframe", "bar_timestamp"],
            unique=False,
        )

    if not inspect(bind).has_table("news_headlines"):
        op.create_table(
            "news_headlines",
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source_name", sa.String(length=120), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("content_hash"),
        )
        op.create_index(
            "ix_news_headlines_published_at",
            "news_headlines",
            ["published_at"],
            unique=False,
        )

    if not inspect(bind).has_table("macro_observations"):
        op.create_table(
            "macro_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("series_id", sa.String(length=32), nullable=False),
            sa.Column("obs_date", sa.String(length=10), nullable=False),
            sa.Column("value", sa.Numeric(precision=24, scale=10), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("series_id", "obs_date", name="uq_macro_series_date"),
        )
        op.create_index(
            "ix_macro_series_date",
            "macro_observations",
            ["series_id", "obs_date"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("macro_observations"):
        op.drop_table("macro_observations")
    if inspect(bind).has_table("news_headlines"):
        op.drop_table("news_headlines")
    if inspect(bind).has_table("feature_snapshots"):
        op.drop_table("feature_snapshots")
