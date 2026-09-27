"""S10 config_versions + investigation freeze columns.

Revision ID: 003_config_versions
Revises: 002_phase2_auth
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_config_versions"
down_revision: Union[str, None] = "002_phase2_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("payload", _Json, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("investigations", sa.Column("knowledge_snapshot_hash", sa.String(128), nullable=True))
    op.add_column("investigations", sa.Column("prompt_version", sa.String(128), nullable=True))
    op.add_column("investigations", sa.Column("model_config_version", sa.String(128), nullable=True))
    op.add_column("investigations", sa.Column("retrieval_config_version", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("investigations", "retrieval_config_version")
    op.drop_column("investigations", "model_config_version")
    op.drop_column("investigations", "prompt_version")
    op.drop_column("investigations", "knowledge_snapshot_hash")
    op.drop_table("config_versions")
