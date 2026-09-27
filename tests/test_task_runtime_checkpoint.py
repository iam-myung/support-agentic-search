"""S11 PG task/checkpointer RED (REQ-007 / AC-009).

Focus (SPEC §3/§5/§8 S11; OP-07/08):
- node idempotency / final-output unique constraint blocks duplicate finals
- after process-kill simulation, task state remains identifiable from PostgreSQL
Redis must not be the sole source of truth.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

PHASE2_TASK_TABLES = frozenset(
    {"task_leases", "investigation_events", "node_executions"}
)
PHASE2_TASK_STATUSES = frozenset(
    {"QUEUED", "RUNNING", "INTERRUPTED", "COMPLETED", "FAILED", "CANCELLED"}
)


def _load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "DATABASE_URL":
                return value.strip().strip('"').strip("'")
    raise AssertionError("DATABASE_URL must be set for S11 PG task/checkpointer contracts")


def _fresh_app_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import support_platform.config as config_mod
    import support_platform.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def _task_runtime_api():
    try:
        from support_platform import task_runtime as tr

        return tr
    except ImportError as exc:
        raise AssertionError(
            "support_platform.task_runtime must exist (S11 task_runtime ownership)"
        ) from exc


def _checkpointer_adapter_cls():
    try:
        from support_platform.task_runtime.checkpointer import (
            PostgresCheckpointerAdapter,
        )

        return PostgresCheckpointerAdapter
    except ImportError as exc:
        raise AssertionError(
            "PostgresCheckpointerAdapter missing under task_runtime.checkpointer"
        ) from exc


# --- Schema / unique constraint (SPEC §3) ---


def test_phase2_task_runtime_metadata_includes_required_tables() -> None:
    from support_platform.infrastructure.db.models import metadata

    present = set(metadata.tables.keys())
    missing = PHASE2_TASK_TABLES - present
    assert not missing, f"Phase 2 task_runtime metadata missing tables: {sorted(missing)}"


def test_node_executions_has_unique_idempotency_key() -> None:
    """领取租约、节点幂等键和最终输出唯一约束共同防止重复最终答案。"""
    from support_platform.infrastructure.db.models import metadata

    assert "node_executions" in metadata.tables, "node_executions table required"
    table = metadata.tables["node_executions"]
    cols = {c.name for c in table.columns}
    for required in ("id", "investigation_id", "idempotency_key", "node_name", "status"):
        assert required in cols, f"node_executions missing column: {required}"

    unique_cols: set[frozenset[str]] = set()
    for constraint in table.constraints:
        col_names = getattr(constraint, "columns", None)
        if col_names is None:
            continue
        names = frozenset(c.name for c in col_names)
        # UniqueConstraint / Index(unique=True)
        is_unique = getattr(constraint, "unique", False) or type(constraint).__name__ == (
            "UniqueConstraint"
        )
        if is_unique and names:
            unique_cols.add(names)
    for index in table.indexes:
        if index.unique:
            unique_cols.add(frozenset(c.name for c in index.columns))

    assert any(
        "idempotency_key" in names or names == frozenset({"investigation_id", "idempotency_key"})
        for names in unique_cols
    ), (
        "node_executions must uniquely constrain idempotency_key "
        f"(alone or with investigation_id); found={unique_cols}"
    )


def test_s11_alembic_revision_file_exists() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    names = [p.name for p in versions.glob("*.py") if p.name != "__init__.py"]
    assert any(
        ("004" in n) or ("checkpoint" in n.lower()) or ("task_runtime" in n.lower())
        for n in names
    ), f"expected S11 Alembic revision (004/checkpoint/task_runtime); found={names}"


# --- Duplicate final output blocked (AC-009 / SPEC §3) ---


def test_duplicate_final_output_blocked_by_unique_constraint() -> None:
    """Second persist of the same final-output idempotency key must not create a second final."""
    tr = _task_runtime_api()
    database_url = _load_database_url()
    investigation_id = uuid4()
    idem_key = f"final:{investigation_id}"

    create = getattr(tr, "create_queued_investigation", None) or getattr(
        tr, "enqueue_investigation", None
    )
    persist_final = getattr(tr, "persist_final_output", None) or getattr(
        tr, "record_final_output", None
    )
    assert callable(create), "task_runtime must expose create_queued_investigation/enqueue"
    assert callable(persist_final), "task_runtime must expose persist/record_final_output"

    create(
        database_url=database_url,
        investigation_id=investigation_id,
        question="S11-RED duplicate final",
        context={},
        owner_user_id=uuid4(),
        knowledge_version_ids=[],
        config_versions={},
    )
    first = persist_final(
        database_url=database_url,
        investigation_id=investigation_id,
        idempotency_key=idem_key,
        result_status="ANSWERED",
        output={"summary": "once"},
        public_steps=[{"step": "compose"}],
    )
    assert first.get("accepted") is True or first.get("task_status") == "COMPLETED"

    second = persist_final(
        database_url=database_url,
        investigation_id=investigation_id,
        idempotency_key=idem_key,
        result_status="ANSWERED",
        output={"summary": "twice-must-not-win"},
        public_steps=[{"step": "compose"}, {"step": "dup"}],
    )
    # Duplicate must be rejected or return the first record — never a second final body.
    assert second.get("accepted") is False or second.get("duplicate") is True or (
        second.get("output", {}).get("summary") == "once"
    ), f"duplicate final must be blocked; got {second}"

    count_fn = getattr(tr, "count_final_outputs", None)
    assert callable(count_fn), "task_runtime.count_final_outputs required for unique-final proof"
    assert count_fn(database_url=database_url, investigation_id=investigation_id) == 1


# --- Kill process → task still identifiable (AC-009 / SPEC §5 Checkpointer) ---


def test_task_state_identifiable_after_process_kill_simulation() -> None:
    """Kill/restart simulation: new process instances must still load task + checkpoint from PG."""
    tr = _task_runtime_api()
    Adapter = _checkpointer_adapter_cls()
    database_url = _load_database_url()
    investigation_id = uuid4()

    create = getattr(tr, "create_queued_investigation", None) or getattr(
        tr, "enqueue_investigation", None
    )
    mark_running = getattr(tr, "mark_running", None)
    get_task = getattr(tr, "get_task", None) or getattr(tr, "load_task", None)
    assert callable(create) and callable(mark_running) and callable(get_task)

    create(
        database_url=database_url,
        investigation_id=investigation_id,
        question="S11-RED kill-process recoverability",
        context={"symptom": "timeout"},
        owner_user_id=uuid4(),
        knowledge_version_ids=[],
        config_versions={},
    )
    mark_running(database_url=database_url, investigation_id=investigation_id)

    adapter_a = Adapter(database_url=database_url)
    thread_id = adapter_a.thread_id_for(investigation_id)
    assert isinstance(thread_id, str) and thread_id, "stable thread_id required"
    adapter_a.put_checkpoint(
        thread_id=thread_id,
        checkpoint_id="ckpt-s11-red-1",
        payload={"node": "retrieve_1", "public_steps": [{"step": "retrieve"}]},
    )

    # Simulate process death: drop in-memory handles, construct fresh adapter/runtime.
    del adapter_a
    adapter_b = Adapter(database_url=database_url)
    restored = adapter_b.get_checkpoint(thread_id=thread_id)
    assert restored is not None, "checkpoint must survive process restart via PostgreSQL"
    assert restored.get("checkpoint_id") == "ckpt-s11-red-1"
    assert restored.get("payload", {}).get("node") == "retrieve_1"

    task = get_task(database_url=database_url, investigation_id=investigation_id)
    assert task is not None, "task row must remain identifiable after process kill"
    assert task["task_status"] in PHASE2_TASK_STATUSES
    assert task["task_status"] in {"RUNNING", "INTERRUPTED", "QUEUED", "FAILED"}, (
        f"post-kill status must be explicit, got {task['task_status']}"
    )
    steps = task.get("public_steps") or restored.get("payload", {}).get("public_steps")
    assert steps, "completed public steps / checkpoint steps must remain recoverable"


# --- OP-07 / OP-08 (SPEC §4) ---


def test_op07_post_creates_queued_investigation(monkeypatch: pytest.MonkeyPatch) -> None:
    from support_platform.auth.testing import ensure_user, login_session

    monkeypatch.setenv("DATABASE_URL", _load_database_url())
    ensure_user(username="s11-agent", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = login_session(client, username="s11-agent", password="secret-ok")
    idem = f"s11-red-{uuid4()}"

    resp = client.post(
        "/api/v2/investigations",
        json={"question": "S11-RED OP-07 create?", "context": {}},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    assert resp.status_code == 202, f"OP-07 must return 202, got {resp.status_code} {resp.text}"
    body = resp.json()
    assert body.get("task_status") == "QUEUED"
    assert body.get("id"), "OP-07 must return investigation id"
    UUID(str(body["id"]))  # valid UUID


def test_op07_idempotent_same_key_same_body_returns_same_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support_platform.auth.testing import ensure_user, login_session

    monkeypatch.setenv("DATABASE_URL", _load_database_url())
    ensure_user(username="s11-agent2", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = login_session(client, username="s11-agent2", password="secret-ok")
    idem = f"s11-red-idem-{uuid4()}"
    payload = {"question": "S11-RED idempotent create", "context": {"product_version": "1.0"}}

    first = client.post(
        "/api/v2/investigations",
        json=payload,
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    second = client.post(
        "/api/v2/investigations",
        json=payload,
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": idem},
    )
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


def test_op08_get_returns_persisted_task_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    from support_platform.auth.testing import ensure_user, login_session

    monkeypatch.setenv("DATABASE_URL", _load_database_url())
    ensure_user(username="s11-agent3", password="secret-ok", role="SUPPORT_AGENT")
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = login_session(client, username="s11-agent3", password="secret-ok")

    created = client.post(
        "/api/v2/investigations",
        json={"question": "S11-RED OP-08 read?", "context": {}},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"s11-op08-{uuid4()}"},
    )
    assert created.status_code == 202, created.text
    inv_id = created.json()["id"]

    got = client.get(f"/api/v2/investigations/{inv_id}", cookies=cookies)
    assert got.status_code == 200, f"OP-08 must return 200 for owner, got {got.status_code}"
    body = got.json()
    assert body.get("id") == inv_id
    assert body.get("task_status") in PHASE2_TASK_STATUSES
    assert "public_steps" in body
    # OP-08 is the final fact source — must not require Redis.
    assert body.get("task_status") == "QUEUED" or body.get("task_status") in PHASE2_TASK_STATUSES
