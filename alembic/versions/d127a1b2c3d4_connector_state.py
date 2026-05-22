"""connector_state

D127 Connect Hub v2 — per-install connector lifecycle state table.
Idempotent for existing DBs.

Revision ID: d127a1b2c3d4
Revises: d126f1a2b3c4
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "d127a1b2c3d4"
down_revision: Union[str, None] = "d126f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("connector_state"):
        return

    op.create_table(
        "connector_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="not_configured"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("certification_tier", sa.String(length=16), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_result", sa.JSON(), nullable=True),
        sa.Column("detected_capabilities", sa.JSON(), nullable=True),
        sa.Column("ai_model_version", sa.String(length=64), nullable=True),
        sa.Column("local_model_install_state", sa.String(length=32), nullable=True),
        sa.Column("machine_probe", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category", "connector_id", name="uq_connector_state_cat_id"),
    )
    op.create_index("ix_connector_state_category", "connector_state", ["category"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("connector_state"):
        op.drop_table("connector_state")
