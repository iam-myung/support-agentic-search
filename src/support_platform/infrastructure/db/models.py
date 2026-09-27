"""SQLAlchemy metadata and table definitions (Phase 1 + Phase 2 auth/config)."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

metadata = MetaData()

_Json = JSON().with_variant(JSONB(), "postgresql")

knowledge_sources = Table(
    "knowledge_sources",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("type", String(64), nullable=False),
    Column("title", String(512), nullable=False),
    Column("status", String(32), nullable=False),
    Column("case_ref", String(128), nullable=True),
    Column("confirmed_resolution", Boolean, nullable=True),
)

knowledge_versions = Table(
    "knowledge_versions",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("source_id", Uuid(as_uuid=True), ForeignKey("knowledge_sources.id"), nullable=False),
    Column("label", String(128), nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("format", String(32), nullable=False),
    Column("parser_version", String(64), nullable=True),
    Column("embedding_model", String(128), nullable=True),
    Column("embedding_dimension", Integer, nullable=True),
    Column("applicability_scope", String(32), nullable=False, server_default="UNKNOWN"),
    Column("product_versions", _Json, nullable=False, server_default="[]"),
    Column("account_types", _Json, nullable=False, server_default="[]"),
    Column("symptom_tags", _Json, nullable=False, server_default="[]"),
    Column("effective_at", DateTime(timezone=True), nullable=True),
    Column("status", String(32), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="false"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

knowledge_chunks = Table(
    "knowledge_chunks",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("version_id", Uuid(as_uuid=True), ForeignKey("knowledge_versions.id"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("locator", _Json, nullable=False),
    # Variable-dimension vector reserved for S2 indexing; stored as opaque for S1 schema.
    Column("embedding", _Json, nullable=True),
    Column("lexical_text", Text, nullable=True),
)

investigations = Table(
    "investigations",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("question", Text, nullable=False),
    Column("context", _Json, nullable=True),
    Column("active_version_ids", _Json, nullable=False, server_default="[]"),
    Column("task_status", String(32), nullable=False),
    Column("result_status", String(32), nullable=True),
    Column("output", _Json, nullable=True),
    Column("public_steps", _Json, nullable=False, server_default="[]"),
    Column("error_code", String(64), nullable=True),
    Column("config_hash", String(128), nullable=True),
    Column("knowledge_snapshot_hash", String(128), nullable=True),
    Column("prompt_version", String(128), nullable=True),
    Column("model_config_version", String(128), nullable=True),
    Column("retrieval_config_version", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("owner_user_id", Uuid(as_uuid=True), nullable=True),
)

config_versions = Table(
    "config_versions",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("payload", _Json, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="false"),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

investigation_evidence = Table(
    "investigation_evidence",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    Column("claim_index", Integer, nullable=False),
    Column("chunk_id", Uuid(as_uuid=True), ForeignKey("knowledge_chunks.id"), nullable=False),
    Column("relation", String(64), nullable=False),
    Column("quote_snapshot", Text, nullable=False),
    Column("locator_snapshot", _Json, nullable=False),
    Column("source_title_snapshot", String(512), nullable=False),
    Column("version_snapshot", _Json, nullable=False),
)

investigation_feedback = Table(
    "investigation_feedback",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    Column("action", String(32), nullable=False),
    Column("reason", Text, nullable=True),
    Column("edited_text", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

users = Table(
    "users",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("username", String(128), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(32), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="true"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("user_id", Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("token_hash", String(128), nullable=False, unique=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("csrf_token", String(128), nullable=False),
)

# Phase 2 S11 — task_runtime ownership (SPEC §3); Redis is not the sole truth.
task_leases = Table(
    "task_leases",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    Column("worker_id", String(128), nullable=True),
    Column("leased_until", DateTime(timezone=True), nullable=True),
    Column("attempt", Integer, nullable=False, server_default="0"),
)

investigation_events = Table(
    "investigation_events",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("type", String(64), nullable=False),
    Column("public_payload", _Json, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("investigation_id", "sequence", name="uq_investigation_events_seq"),
)

node_executions = Table(
    "node_executions",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("node_name", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_payload", _Json, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "investigation_id",
        "idempotency_key",
        name="uq_node_executions_inv_idempotency",
    ),
)

graph_checkpoints = Table(
    "graph_checkpoints",
    metadata,
    Column("thread_id", String(128), primary_key=True),
    Column("checkpoint_id", String(128), nullable=False),
    Column("payload", _Json, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

investigation_create_keys = Table(
    "investigation_create_keys",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("owner_user_id", Uuid(as_uuid=True), nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("body_hash", String(128), nullable=False),
    Column("investigation_id", Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False),
    UniqueConstraint(
        "owner_user_id",
        "idempotency_key",
        name="uq_investigation_create_keys_owner_key",
    ),
)

# Phase 2 S16 — sanitized audit only (SPEC §3); no question/secret/reasoning columns.
audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("action", String(64), nullable=False),
    Column("actor", String(256), nullable=False),
    Column("investigation_id", Uuid(as_uuid=True), nullable=True),
    Column("request_id", String(128), nullable=True),
    Column("payload", _Json, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
