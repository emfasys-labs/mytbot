"""add control plane tables

Revision ID: a1d9c3b2e770
Revises: f27c0a1b9e10
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa


revision = "a1d9c3b2e770"
down_revision = "f27c0a1b9e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("control_commands"):
        op.create_table(
            "control_commands",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("command_type", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    cmd_indexes = {ix.get("name") for ix in inspector.get_indexes("control_commands")}
    if "ix_control_commands_command_type" not in cmd_indexes:
        op.create_index("ix_control_commands_command_type", "control_commands", ["command_type"], unique=False)
    if "ix_control_commands_status" not in cmd_indexes:
        op.create_index("ix_control_commands_status", "control_commands", ["status"], unique=False)
    if "ix_control_commands_created_at" not in cmd_indexes:
        op.create_index("ix_control_commands_created_at", "control_commands", ["created_at"], unique=False)

    if not inspector.has_table("control_state"):
        op.create_table(
            "control_state",
            sa.Column("key", sa.String(length=128), nullable=False),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )
    st_indexes = {ix.get("name") for ix in inspector.get_indexes("control_state")}
    if "ix_control_state_updated_at" not in st_indexes:
        op.create_index("ix_control_state_updated_at", "control_state", ["updated_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("control_state"):
        st_indexes = {ix.get("name") for ix in inspector.get_indexes("control_state")}
        if "ix_control_state_updated_at" in st_indexes:
            op.drop_index("ix_control_state_updated_at", table_name="control_state")
        op.drop_table("control_state")
    if inspector.has_table("control_commands"):
        cmd_indexes = {ix.get("name") for ix in inspector.get_indexes("control_commands")}
        if "ix_control_commands_created_at" in cmd_indexes:
            op.drop_index("ix_control_commands_created_at", table_name="control_commands")
        if "ix_control_commands_status" in cmd_indexes:
            op.drop_index("ix_control_commands_status", table_name="control_commands")
        if "ix_control_commands_command_type" in cmd_indexes:
            op.drop_index("ix_control_commands_command_type", table_name="control_commands")
        op.drop_table("control_commands")
