"""Wave 1 — model registry, prediction store, feature contracts.

Adds the five tables that govern trained models:
- ``feature_contracts``
- ``training_datasets``
- ``model_versions``
- ``model_runs``
- ``model_predictions``

Idempotent — every table creation is gated on ``inspect(bind).has_table(...)``
so re-running on a database that already has them is a no-op.

Revision ID: b3a07f1e9c10
Revises: a2b3c4d5e6f7
Create Date: 2026-04-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "b3a07f1e9c10"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if not insp.has_table("feature_contracts"):
        op.create_table(
            "feature_contracts",
            sa.Column("hash", sa.String(length=64), nullable=False),
            sa.Column("feature_list", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("hash"),
        )

    if not insp.has_table("training_datasets"):
        op.create_table(
            "training_datasets",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("start_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.Column("feature_contract_hash", sa.String(length=64), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", "version", name="uq_training_datasets_name_version"),
        )
        op.create_index(
            "ix_training_datasets_name",
            "training_datasets",
            ["name"],
            unique=False,
        )
        op.create_index(
            "ix_training_datasets_fc_hash",
            "training_datasets",
            ["feature_contract_hash"],
            unique=False,
        )

    if not insp.has_table("model_versions"):
        op.create_table(
            "model_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("task", sa.String(length=32), nullable=False),
            sa.Column("target", sa.String(length=128), nullable=False),
            sa.Column("horizon_seconds", sa.Integer(), nullable=True),
            sa.Column("horizon_bars", sa.Integer(), nullable=True),
            sa.Column("feature_contract_hash", sa.String(length=64), nullable=False),
            sa.Column("training_dataset_id", sa.Integer(), nullable=True),
            sa.Column("validation_method", sa.String(length=64), nullable=False),
            sa.Column(
                "calibration_method",
                sa.String(length=32),
                nullable=False,
                server_default="none",
            ),
            sa.Column("min_sample_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "approval_status",
                sa.String(length=20),
                nullable=False,
                server_default="research",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "created_by",
                sa.String(length=64),
                nullable=False,
                server_default="system",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        )
        op.create_index(
            "ix_model_versions_name", "model_versions", ["name"], unique=False
        )
        op.create_index(
            "ix_model_versions_fc_hash",
            "model_versions",
            ["feature_contract_hash"],
            unique=False,
        )
        op.create_index(
            "ix_model_versions_status",
            "model_versions",
            ["approval_status"],
            unique=False,
        )
        op.create_index(
            "ix_model_versions_name_status",
            "model_versions",
            ["name", "approval_status"],
            unique=False,
        )
        op.create_index(
            "ix_model_versions_training_dataset",
            "model_versions",
            ["training_dataset_id"],
            unique=False,
        )

    if not insp.has_table("model_runs"):
        op.create_table(
            "model_runs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("model_name", sa.String(length=128), nullable=False),
            sa.Column("model_version", sa.String(length=32), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="running",
            ),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_model_runs_model_name", "model_runs", ["model_name"], unique=False
        )
        op.create_index(
            "ix_model_runs_name_version_kind",
            "model_runs",
            ["model_name", "model_version", "kind"],
            unique=False,
        )

    if not insp.has_table("model_predictions"):
        op.create_table(
            "model_predictions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("model_name", sa.String(length=128), nullable=False),
            sa.Column("model_version", sa.String(length=32), nullable=False),
            sa.Column("symbol", sa.String(length=72), nullable=False),
            sa.Column("as_of_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("prediction_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("horizon_seconds", sa.Integer(), nullable=True),
            sa.Column("horizon_bars", sa.Integer(), nullable=True),
            sa.Column("predicted_probability", sa.Numeric(precision=10, scale=8), nullable=True),
            sa.Column("expected_return", sa.Numeric(precision=20, scale=10), nullable=True),
            sa.Column("expected_volatility", sa.Numeric(precision=20, scale=10), nullable=True),
            sa.Column("confidence", sa.Numeric(precision=10, scale=8), nullable=True),
            sa.Column("feature_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "mode",
                sa.String(length=16),
                nullable=False,
                server_default="research",
            ),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_model_pred_model_name",
            "model_predictions",
            ["model_name"],
            unique=False,
        )
        op.create_index(
            "ix_model_pred_symbol",
            "model_predictions",
            ["symbol"],
            unique=False,
        )
        op.create_index(
            "ix_model_pred_feature_hash",
            "model_predictions",
            ["feature_hash"],
            unique=False,
        )
        op.create_index(
            "ix_model_pred_name_version_ts",
            "model_predictions",
            ["model_name", "model_version", "prediction_ts"],
            unique=False,
        )
        op.create_index(
            "ix_model_pred_symbol_ts",
            "model_predictions",
            ["symbol", "prediction_ts"],
            unique=False,
        )


def downgrade() -> None:
    # Wave 1 tables; safe to drop in reverse order.
    op.drop_table("model_predictions")
    op.drop_table("model_runs")
    op.drop_table("model_versions")
    op.drop_table("training_datasets")
    op.drop_table("feature_contracts")
