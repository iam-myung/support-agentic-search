"""S11 task_runtime tables + PG checkpointer storage.

Revision ID: 004_task_runtime_checkpoint
Revises: 003_config_versions
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004_task_runtime_checkpoint"
down_revision: Union[str, None] = "003_config_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "task_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "investigation_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("public_payload", _Json, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_events_seq"),
    )
    op.create_table(
        "node_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("node_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_payload", _Json, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_node_executions_inv_idempotency",
        ),
    )
    op.create_table(
        "graph_checkpoints",
        sa.Column("thread_id", sa.String(128), primary_key=True),
        sa.Column("checkpoint_id", sa.String(128), nullable=False),
        sa.Column("payload", _Json, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "investigation_create_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("body_hash", sa.String(128), nullable=False),
        sa.Column(
            "investigation_id",
            sa.Uuid(),
            sa.ForeignKey("investigations.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_investigation_create_keys_owner_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("investigation_create_keys")
    op.drop_table("graph_checkpoints")
    op.drop_table("node_executions")
    op.drop_table("investigation_events")
    op.drop_table("task_leases")
