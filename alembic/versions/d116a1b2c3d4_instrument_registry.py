"""D116 — instrument registry + cross-broker availability tables.

Adds four tables that back the instrument master / availability resolver:

- ``instrument_registry``
- ``instrument_source_membership``
- ``instrument_broker_availability``
- ``instrument_source_runs``

Idempotent: every ``create_table`` is gated on ``inspect(bind).has_table(...)``
so re-running on a database that already has them is a no-op.

Revision ID: d116a1b2c3d4
Revises: b3a07f1e9c10
Create Date: 2026-05-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "d116a1b2c3d4"
down_revision: Union[str, None] = "b3a07f1e9c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if not insp.has_table("instrument_registry"):
        op.create_table(
            "instrument_registry",
            sa.Column("canonical_symbol", sa.String(length=72), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            sa.Column("asset_class", sa.String(length=20), nullable=False),
            sa.Column("region", sa.String(length=16), nullable=True),
            sa.Column("exchange", sa.String(length=32), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("sector", sa.String(length=64), nullable=True),
            sa.Column("industry", sa.String(length=64), nullable=True),
            sa.Column("isin", sa.String(length=16), nullable=True),
            sa.Column("figi", sa.String(length=16), nullable=True),
            sa.Column(
                "first_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("canonical_symbol"),
        )
        op.create_index("ix_instrument_registry_asset_class", "instrument_registry", ["asset_class"])
        op.create_index("ix_instrument_registry_region", "instrument_registry", ["region"])
        op.create_index("ix_instrument_registry_isin", "instrument_registry", ["isin"])
        op.create_index("ix_instrument_registry_figi", "instrument_registry", ["figi"])
        op.create_index("ix_instrument_registry_retired_at", "instrument_registry", ["retired_at"])

    if not insp.has_table("instrument_source_membership"):
        op.create_table(
            "instrument_source_membership",
            sa.Column("canonical_symbol", sa.String(length=72), nullable=False),
            sa.Column("source_id", sa.String(length=64), nullable=False),
            sa.Column("source_version", sa.String(length=32), nullable=True),
            sa.Column("external_id", sa.String(length=64), nullable=True),
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "consecutive_miss_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("canonical_symbol", "source_id"),
        )
        op.create_index(
            "ix_instrument_source_membership_source",
            "instrument_source_membership",
            ["source_id"],
        )
        op.create_index(
            "ix_instrument_source_membership_symbol_source",
            "instrument_source_membership",
            ["canonical_symbol", "source_id"],
            unique=True,
        )

    if not insp.has_table("instrument_broker_availability"):
        op.create_table(
            "instrument_broker_availability",
            sa.Column("canonical_symbol", sa.String(length=72), nullable=False),
            sa.Column("broker", sa.String(length=20), nullable=False),
            sa.Column("broker_symbol", sa.String(length=72), nullable=True),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="unknown",
            ),
            sa.Column(
                "last_checked_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("last_available_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("qualification_payload", sa.JSON(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("canonical_symbol", "broker"),
        )
        op.create_index(
            "ix_instrument_broker_availability_broker",
            "instrument_broker_availability",
            ["broker"],
        )
        op.create_index(
            "ix_instrument_broker_availability_status",
            "instrument_broker_availability",
            ["status"],
        )

    if not insp.has_table("instrument_source_runs"):
        op.create_table(
            "instrument_source_runs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("source_id", sa.String(length=64), nullable=False),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="running",
            ),
            sa.Column("rows_added", sa.Integer(), nullable=True),
            sa.Column("rows_updated", sa.Integer(), nullable=True),
            sa.Column("rows_missing", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_instrument_source_runs_source_id",
            "instrument_source_runs",
            ["source_id"],
        )
        op.create_index(
            "ix_instrument_source_runs_source_started",
            "instrument_source_runs",
            ["source_id", "started_at"],
        )


def downgrade() -> None:
    op.drop_table("instrument_source_runs")
    op.drop_table("instrument_broker_availability")
    op.drop_table("instrument_source_membership")
    op.drop_table("instrument_registry")
