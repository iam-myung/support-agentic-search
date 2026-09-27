"""S16 Trace / audit RED contracts (REQ-010 / AC-012).

Focus (SPEC §5 Trace whitelist; §3 audit_events; §8 S16):
- audit_events table + migration; no secret columns
- Trace accepts only whitelist fields (+ human decision type/actor)
- rejects / strips raw question, full docs, secrets, internal reasoning
- investigation version snapshot fields are readable via Trace lookup
- human decisions and login-failure write sanitized audit rows
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

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
        # Human decision fields allowed by SPEC §5
        "decision_type",
        "actor",
    }
)

FORBIDDEN_MARKERS = (
    "customer raw question text",
    "FULL DOCUMENT BODY DUMP",
    "sk-secret-key-leak",
    "internal chain of thought reasoning",
    "api_key",
    "LLM_API_KEY",
)

SECRET_COLUMN_NAMES = frozenset(
    {
        "api_key",
        "password",
        "secret",
        "token",
        "question",
        "raw_document",
        "reasoning",
        "llm_api_key",
    }
)


def _audit_module():
    try:
        import support_platform.audit as audit_mod

        return audit_mod
    except ImportError as exc:
        pytest.fail(f"S16 audit package missing: {exc}")


def _trace_recorder_cls():
    try:
        from support_platform.audit.trace import TraceRecorder

        return TraceRecorder
    except ImportError as exc:
        pytest.fail(f"S16 TraceRecorder missing: {exc}")


def _audit_store_cls():
    try:
        from support_platform.audit.store import AuditStore

        return AuditStore
    except ImportError as exc:
        pytest.fail(f"S16 AuditStore missing: {exc}")


def test_metadata_includes_audit_events_table() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "audit_events" in metadata.tables, (
        "SPEC §3 requires audit_events for sanitized action audit"
    )


def test_audit_events_table_has_required_columns_without_secrets() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "audit_events" in metadata.tables
    cols = {c.name for c in metadata.tables["audit_events"].columns}
    for required in (
        "id",
        "action",
        "actor",
        "investigation_id",
        "request_id",
        "payload",
        "created_at",
    ):
        assert required in cols, f"audit_events missing column: {required}"
    secret_cols = cols & SECRET_COLUMN_NAMES
    assert not secret_cols, f"audit_events must not store secret/body columns: {secret_cols}"


def test_alembic_audit_events_revision_exists() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    names = [p.name for p in versions.glob("*.py") if p.name != "__init__.py"]
    assert any(
        ("005" in n) or ("audit" in n.lower()) for n in names
    ), f"expected audit_events Alembic revision (005); found={names}"


def test_trace_recorder_exposes_record_and_list_by_investigation() -> None:
    TraceRecorder = _trace_recorder_cls()
    assert hasattr(TraceRecorder, "record"), "TraceRecorder.record required"
    assert hasattr(TraceRecorder, "list_for_investigation"), (
        "TraceRecorder.list_for_investigation required for AC-012 replay"
    )


def test_trace_rejects_forbidden_sensitive_fields() -> None:
    """Leakage cases must fail closed — no raw question / docs / secrets / reasoning."""
    TraceRecorder = _trace_recorder_cls()
    recorder = TraceRecorder(backend="memory")
    inv_id = str(uuid4())
    request_id = str(uuid4())

    with pytest.raises((ValueError, TypeError, AssertionError)):
        recorder.record(
            {
                "request_id": request_id,
                "investigation_id": inv_id,
                "node_name": "retrieve",
                "step_status": "COMPLETED",
                "question": "customer raw question text",
                "document_body": "FULL DOCUMENT BODY DUMP",
                "api_key": "sk-secret-key-leak",
                "reasoning": "internal chain of thought reasoning",
            }
        )

    # Even if caller sneaks forbidden keys via nested payload, sanitized output
    # must not retain forbidden markers when a sanitizer helper is used.
    audit = _audit_module()
    assert hasattr(audit, "sanitize_trace_fields"), (
        "audit.sanitize_trace_fields required to strip non-whitelist keys"
    )
    cleaned = audit.sanitize_trace_fields(
        {
            "request_id": request_id,
            "investigation_id": inv_id,
            "node_name": "retrieve",
            "step_status": "COMPLETED",
            "knowledge_snapshot_hash": "abc",
            "prompt_version": "p1",
            "model_config_version": "m1",
            "retrieval_config_version": "r1",
            "question": "customer raw question text",
            "api_key": "sk-secret-key-leak",
            "reasoning": "internal chain of thought reasoning",
            "document_body": "FULL DOCUMENT BODY DUMP",
        }
    )
    blob = str(cleaned).lower()
    for marker in FORBIDDEN_MARKERS:
        assert marker.lower() not in blob, f"sanitized Trace leaked marker: {marker}"
    for key in cleaned:
        assert key in TRACE_WHITELIST, f"non-whitelist Trace key survived: {key}"


def test_trace_whitelist_fields_round_trip_by_investigation() -> None:
    TraceRecorder = _trace_recorder_cls()
    recorder = TraceRecorder(backend="memory")
    inv_id = str(uuid4())
    request_id = str(uuid4())
    payload = {
        "request_id": request_id,
        "investigation_id": inv_id,
        "node_name": "evidence_assess",
        "step_status": "COMPLETED",
        "knowledge_snapshot_hash": "ks-hash-1",
        "prompt_version": "prompt-v1",
        "model_config_version": "model-v1",
        "retrieval_config_version": "retrieval-v1",
        "source_ids": ["src-a"],
        "latency_ms": 12,
        "token_usage": {"prompt": 1, "completion": 2},
        "error_code": None,
    }
    recorder.record(payload)
    rows = recorder.list_for_investigation(inv_id)
    assert rows, "Trace list_for_investigation must return recorded steps"
    row = rows[0]
    for key in (
        "knowledge_snapshot_hash",
        "prompt_version",
        "model_config_version",
        "retrieval_config_version",
        "node_name",
        "step_status",
    ):
        assert row.get(key) == payload[key], f"Trace missing/wrong {key}"
    blob = str(row).lower()
    for marker in FORBIDDEN_MARKERS:
        assert marker.lower() not in blob


def test_audit_store_records_human_decision_without_success_body() -> None:
    AuditStore = _audit_store_cls()
    store = AuditStore(backend="memory")
    inv_id = str(uuid4())
    store.append(
        action="HITL_REJECT",
        actor="agent-1",
        investigation_id=inv_id,
        request_id=str(uuid4()),
        payload={"decision_type": "REJECT"},
    )
    rows = store.list_for_investigation(inv_id)
    assert rows, "audit must retain human decision"
    row = rows[0]
    assert row["action"] == "HITL_REJECT"
    assert row["actor"] == "agent-1"
    assert row.get("payload", {}).get("decision_type") == "REJECT"
    # Reject must not be recorded as execution success.
    assert row["action"] not in {"EXECUTE_SUCCESS", "TOOL_SUCCESS", "ANSWERED"}
    blob = str(row).lower()
    assert "customer raw question text" not in blob
    assert "sk-secret-key-leak" not in blob


def test_audit_store_records_login_failure_sanitized() -> None:
    AuditStore = _audit_store_cls()
    store = AuditStore(backend="memory")
    store.append(
        action="LOGIN_FAILURE",
        actor="unknown",
        investigation_id=None,
        request_id=str(uuid4()),
        payload={"username": "bob", "password": "should-not-persist", "api_key": "sk-x"},
    )
    rows = store.list_by_action("LOGIN_FAILURE")
    assert rows, "OP-12 login failure must write sanitized audit"
    row = rows[0]
    blob = str(row).lower()
    assert "should-not-persist" not in blob
    assert "sk-x" not in blob
    assert "password" not in str(row.get("payload", {})).lower() or (
        row.get("payload", {}).get("password") in (None, "", "[REDACTED]")
    )


def test_auth_login_failure_hooks_audit_write() -> None:
    """Wiring probe: auth login path must call into audit on failure."""
    try:
        from support_platform.auth import routes as auth_routes
    except ImportError as exc:
        pytest.fail(f"auth.routes missing: {exc}")
    src = Path(auth_routes.__file__).read_text(encoding="utf-8")
    assert "audit" in src.lower() or "AuditStore" in src or "LOGIN_FAILURE" in src, (
        "OP-12 login failure must hook sanitized audit write (SPEC §4 OP-12)"
    )


def test_investigation_or_task_runtime_hooks_trace_write() -> None:
    """Wiring probe: at least one investigation/task_runtime module records Trace."""
    candidates = [
        Path("src/support_platform/investigation/graph.py"),
        Path("src/support_platform/investigation/hitl.py"),
        Path("src/support_platform/task_runtime/worker.py"),
        Path("src/support_platform/task_runtime/service.py"),
    ]
    repo = Path(__file__).resolve().parents[1]
    hits = []
    for rel in candidates:
        path = repo / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "TraceRecorder" in text or "audit.trace" in text or "record_trace" in text:
            hits.append(rel.as_posix())
    assert hits, (
        "Trace write hook missing in investigation/task_runtime "
        "(expected TraceRecorder / audit.trace / record_trace)"
    )
