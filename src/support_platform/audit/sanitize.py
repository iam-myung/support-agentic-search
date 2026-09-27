"""Trace / audit field sanitizers (SPEC §5 whitelist; no raw body/secrets)."""

from __future__ import annotations

from typing import Any

TRACE_WHITELIST = frozenset(
    {
        "request_id",
        "investigation_id",
        "node_name",
        "step_status",
        "knowledge_snapshot_hash",
        "prompt_version",
        "model_config_version",
        "retrieval_config_version",
        "source_ids",
        "latency_ms",
        "token_usage",
        "error_code",
        "decision_type",
        "actor",
    }
)

_FORBIDDEN_TRACE_KEYS = frozenset(
    {
        "question",
        "document_body",
        "raw_document",
        "api_key",
        "apikey",
        "llm_api_key",
        "embedding_api_key",
        "password",
        "secret",
        "token",
        "reasoning",
        "internal_reasoning",
        "chain_of_thought",
        "cot",
        "system_prompt",
        "prompt",
    }
)

_AUDIT_DROP_KEYS = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "llm_api_key",
        "embedding_api_key",
        "secret",
        "token",
        "reasoning",
        "question",
        "document_body",
        "raw_document",
    }
)


def sanitize_trace_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only SPEC §5 Trace whitelist keys."""
    if not payload:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in TRACE_WHITELIST:
            continue
        cleaned[key] = value
    return cleaned


def has_forbidden_trace_keys(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    for key in payload:
        lk = str(key).lower().replace("-", "_")
        if key not in TRACE_WHITELIST and (
            key in _FORBIDDEN_TRACE_KEYS or lk in _FORBIDDEN_TRACE_KEYS
        ):
            return True
        if lk in {"question", "document_body", "reasoning", "api_key", "password"}:
            return True
    return False


def sanitize_audit_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Drop secrets / sensitive body from audit payloads."""
    if not payload:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lk = str(key).lower().replace("-", "_")
        if lk in _AUDIT_DROP_KEYS or any(
            part in lk for part in ("password", "api_key", "secret", "token", "reasoning")
        ):
            continue
        if isinstance(value, str):
            low = value.lower()
            if "sk-" in low or "chain of thought" in low:
                continue
        if isinstance(value, dict):
            cleaned[key] = sanitize_audit_payload(value)
        else:
            cleaned[key] = value
    return cleaned
