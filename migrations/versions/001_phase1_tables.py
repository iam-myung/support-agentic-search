"""Phase 1 core tables.

Revision ID: 001_phase1
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_phase1"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("case_ref", sa.String(128), nullable=True),
        sa.Column("confirmed_resolution", sa.Boolean(), nullable=True),
    )
    op.create_table(
        "knowledge_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("knowledge_sources.id"), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("applicability_scope", sa.String(32), nullable=False, server_default="UNKNOWN"),
        sa.Column("product_versions", _Json, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("account_types", _Json, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("symptom_tags", _Json, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("knowledge_versions.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("locator", _Json, nullable=False),
        sa.Column("embedding", _Json, nullable=True),
        sa.Column("lexical_text", sa.Text(), nullable=True),
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", _Json, nullable=True),
        sa.Column("active_version_ids", _Json, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("task_status", sa.String(32), nullable=False),
        sa.Column("result_status", sa.String(32), nullable=True),
        sa.Column("output", _Json, nullable=True),
        sa.Column("public_steps", _Json, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("config_hash", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "investigation_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("claim_index", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), sa.ForeignKey("knowledge_chunks.id"), nullable=False),
        sa.Column("relation", sa.String(64), nullable=False),
        sa.Column("quote_snapshot", sa.Text(), nullable=False),
        sa.Column("locator_snapshot", _Json, nullable=False),
        sa.Column("source_title_snapshot", sa.String(512), nullable=False),
        sa.Column("version_snapshot", _Json, nullable=False),
    )
    op.create_table(
        "investigation_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("edited_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("investigation_feedback")
    op.drop_table("investigation_evidence")
    op.drop_table("investigations")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_versions")
    op.drop_table("knowledge_sources")
