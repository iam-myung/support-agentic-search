"""HITL interrupt / resume / cancel (SPEC §4 OP-10/11; §5 interrupt + idempotency)."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from support_platform.audit.store import AuditStore
from support_platform.audit.trace import TraceRecorder, record_trace
from support_platform.task_runtime.checkpointer import PostgresCheckpointerAdapter
from support_platform.task_runtime.service import get_task

ALLOWED_ACTIONS = frozenset({"SUPPLY", "APPROVE", "EDIT", "REJECT"})
RESUME_NODE = "hitl_resume"
CANCEL_NODE = "hitl_cancel"


def _audit_human_decision(
    *,
    investigation_id: str,
    action: str,
    database_url: str,
    actor: str = "support_agent",
) -> None:
    """Append sanitized HITL audit + whitelist Trace (AC-012)."""
    request_id = str(uuid4())
    AuditStore(backend="postgres", database_url=database_url).append(
        action=f"HITL_{action}",
        actor=actor,
        investigation_id=investigation_id,
        request_id=request_id,
        payload={"decision_type": action},
    )
    # Pull frozen version fields from the investigation row when present.
    task = get_task(database_url=database_url, investigation_id=investigation_id) or {}
    record_trace(
        {
            "request_id": request_id,
            "investigation_id": investigation_id,
            "node_name": RESUME_NODE,
            "step_status": "COMPLETED",
            "decision_type": action,
            "actor": actor,
            "knowledge_snapshot_hash": task.get("knowledge_snapshot_hash"),
            "prompt_version": task.get("prompt_version"),
            "model_config_version": task.get("model_config_version"),
            "retrieval_config_version": task.get("retrieval_config_version"),
            "error_code": "HUMAN_REJECTED" if action == "REJECT" else None,
        },
        backend="postgres",
        database_url=database_url,
    )
    # Keep TraceRecorder symbol referenced for wiring probes / future callers.
    _ = TraceRecorder


def _engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def stable_interrupt_id(*, investigation_id: str | UUID, reason: str = "human_needed") -> str:
    raw = f"{investigation_id}:{reason}".encode("utf-8")
    return f"intr-{hashlib.sha256(raw).hexdigest()[:16]}"


def extract_interrupt_id(task_or_steps: dict[str, Any] | list[dict[str, Any]] | None) -> str | None:
    if task_or_steps is None:
        return None
    if isinstance(task_or_steps, list):
        steps = task_or_steps
        direct = None
    else:
        direct = task_or_steps.get("interrupt_id")
        if direct:
            return str(direct)
        steps = list(task_or_steps.get("public_steps") or [])
    for step in reversed(steps):
        iid = step.get("interrupt_id")
        if iid:
            return str(iid)
    return None


def body_hash_for_resume(
    *,
    interrupt_id: str,
    action: str,
    input_payload: dict[str, Any] | None,
) -> str:
    raw = json.dumps(
        {
            "interrupt_id": interrupt_id,
            "action": action,
            "input": input_payload or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup_resume_idempotency(
    *,
    database_url: str,
    investigation_id: str | UUID,
    idempotency_key: str,
) -> dict[str, Any] | None:
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT status, result_payload
                FROM node_executions
                WHERE investigation_id = CAST(:iid AS uuid)
                  AND idempotency_key = :ikey
                  AND node_name = :node
                """
            ),
            {
                "iid": str(investigation_id),
                "ikey": idempotency_key,
                "node": RESUME_NODE,
            },
        ).mappings().first()
    if row is None:
        return None
    payload = row["result_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return dict(payload or {})


def remember_resume_idempotency(
    *,
    database_url: str,
    investigation_id: str | UUID,
    idempotency_key: str,
    body_hash: str,
    response: dict[str, Any],
) -> None:
    payload = {"body_hash": body_hash, "response": response}
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO node_executions (
                  id, investigation_id, idempotency_key, node_name, status, result_payload
                ) VALUES (
                  CAST(:nid AS uuid), CAST(:iid AS uuid), :ikey, :node,
                  'COMPLETED', CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "nid": str(uuid4()),
                "iid": str(investigation_id),
                "ikey": idempotency_key,
                "node": RESUME_NODE,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )


def lookup_cancel_idempotency(
    *,
    database_url: str,
    investigation_id: str | UUID,
    idempotency_key: str,
) -> dict[str, Any] | None:
    with _engine(database_url).connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT result_payload
                FROM node_executions
                WHERE investigation_id = CAST(:iid AS uuid)
                  AND idempotency_key = :ikey
                  AND node_name = :node
                """
            ),
            {
                "iid": str(investigation_id),
                "ikey": idempotency_key,
                "node": CANCEL_NODE,
            },
        ).mappings().first()
    if row is None:
        return None
    payload = row["result_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return dict(payload or {})


def remember_cancel_idempotency(
    *,
    database_url: str,
    investigation_id: str | UUID,
    idempotency_key: str,
    response: dict[str, Any],
) -> None:
    try:
        with _engine(database_url).begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO node_executions (
                      id, investigation_id, idempotency_key, node_name, status, result_payload
                    ) VALUES (
                      CAST(:nid AS uuid), CAST(:iid AS uuid), :ikey, :node,
                      'COMPLETED', CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "nid": str(uuid4()),
                    "iid": str(investigation_id),
                    "ikey": idempotency_key,
                    "node": CANCEL_NODE,
                    "payload": json.dumps({"response": response}, ensure_ascii=False),
                },
            )
    except IntegrityError:
        return


def mark_interrupted(
    *,
    database_url: str,
    investigation_id: str | UUID,
    reason: str = "human_needed",
    public_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist INTERRUPTED + stable interrupt_id + checkpoint. Idempotent while interrupted."""
    iid = str(investigation_id)
    task = get_task(database_url=database_url, investigation_id=iid)
    if task is None:
        raise ValueError(f"investigation not found: {iid}")

    if task.get("task_status") == "INTERRUPTED":
        existing = extract_interrupt_id(task)
        if existing:
            return {"interrupt_id": existing, "task_status": "INTERRUPTED", "id": iid}
        adapter = PostgresCheckpointerAdapter(database_url=database_url)
        ckpt = adapter.get_checkpoint(thread_id=iid)
        if ckpt and isinstance(ckpt.get("payload"), dict):
            existing = ckpt["payload"].get("interrupt_id")
            if existing:
                return {"interrupt_id": str(existing), "task_status": "INTERRUPTED", "id": iid}

    interrupt_id = stable_interrupt_id(investigation_id=iid, reason=reason)
    steps = list(public_steps if public_steps is not None else (task.get("public_steps") or []))
    if not any(s.get("interrupt_id") == interrupt_id for s in steps):
        steps.append(
            {
                "kind": "review_required",
                "interrupt_id": interrupt_id,
                "reason": reason,
            }
        )

    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'INTERRUPTED',
                    result_status = NULL,
                    public_steps = CAST(:steps AS jsonb)
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": iid, "steps": json.dumps(steps, ensure_ascii=False)},
        )

    adapter = PostgresCheckpointerAdapter(database_url=database_url)
    adapter.put_checkpoint(
        thread_id=iid,
        checkpoint_id=f"ckpt-hitl-{interrupt_id}",
        payload={
            "interrupt_id": interrupt_id,
            "public_steps": steps,
            "awaiting": "human_decision",
            "reason": reason,
        },
    )
    return {"interrupt_id": interrupt_id, "task_status": "INTERRUPTED", "id": iid}


def _current_interrupt_id(*, database_url: str, investigation_id: str) -> str | None:
    task = get_task(database_url=database_url, investigation_id=investigation_id)
    if task is None:
        return None
    found = extract_interrupt_id(task)
    if found:
        return found
    adapter = PostgresCheckpointerAdapter(database_url=database_url)
    ckpt = adapter.get_checkpoint(thread_id=investigation_id)
    if ckpt and isinstance(ckpt.get("payload"), dict):
        iid = ckpt["payload"].get("interrupt_id")
        if iid:
            return str(iid)
    return None


def apply_human_decision(
    *,
    database_url: str,
    investigation_id: str | UUID,
    interrupt_id: str,
    action: str,
    input_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply SUPPLY/APPROVE/EDIT/REJECT. Returns response dict or raises ConflictError."""
    iid = str(investigation_id)
    action_u = (action or "").strip().upper()
    if action_u not in ALLOWED_ACTIONS:
        raise ValueError(f"invalid action: {action}")

    bh = body_hash_for_resume(
        interrupt_id=interrupt_id,
        action=action_u,
        input_payload=input_payload,
    )

    if idempotency_key:
        prior = lookup_resume_idempotency(
            database_url=database_url,
            investigation_id=iid,
            idempotency_key=idempotency_key,
        )
        if prior is not None:
            if prior.get("body_hash") != bh:
                raise ConflictError("idempotency key conflict")
            resp = dict(prior.get("response") or {})
            resp.setdefault("id", iid)
            return resp

    task = get_task(database_url=database_url, investigation_id=iid)
    if task is None:
        raise LookupError("not found")

    if task.get("task_status") != "INTERRUPTED":
        raise ConflictError("not interrupted")

    current = _current_interrupt_id(database_url=database_url, investigation_id=iid)
    if not current or current != interrupt_id:
        raise ConflictError("interrupt_id mismatch")

    steps = list(task.get("public_steps") or [])
    steps.append(
        {
            "kind": "human_decision",
            "action": action_u,
            "interrupt_id": interrupt_id,
        }
    )

    if action_u == "REJECT":
        # Terminate without execution success — no final_output row.
        with _engine(database_url).begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE investigations
                    SET task_status = 'CANCELLED',
                        result_status = 'NEEDS_HUMAN',
                        public_steps = CAST(:steps AS jsonb),
                        error_code = 'HUMAN_REJECTED'
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": iid, "steps": json.dumps(steps, ensure_ascii=False)},
            )
        response = {
            "id": iid,
            "task_status": "CANCELLED",
            "result_status": "NEEDS_HUMAN",
            "action": action_u,
        }
    else:
        # SUPPLY / APPROVE / EDIT → re-queue for continuation from checkpoint.
        context = dict(task.get("context") or {})
        if input_payload:
            context["human_input"] = input_payload
            context["human_action"] = action_u
        with _engine(database_url).begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE investigations
                    SET task_status = 'QUEUED',
                        result_status = NULL,
                        public_steps = CAST(:steps AS jsonb),
                        context = CAST(:ctx AS jsonb),
                        error_code = NULL
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": iid,
                    "steps": json.dumps(steps, ensure_ascii=False),
                    "ctx": json.dumps(context, ensure_ascii=False),
                },
            )
        response = {"id": iid, "task_status": "QUEUED", "action": action_u}
        _maybe_wake(database_url=database_url, investigation_id=iid)

    _audit_human_decision(
        investigation_id=iid,
        action=action_u,
        database_url=database_url,
    )

    if idempotency_key:
        try:
            remember_resume_idempotency(
                database_url=database_url,
                investigation_id=iid,
                idempotency_key=idempotency_key,
                body_hash=bh,
                response=response,
            )
        except IntegrityError as exc:
            prior = lookup_resume_idempotency(
                database_url=database_url,
                investigation_id=iid,
                idempotency_key=idempotency_key,
            )
            if prior and prior.get("body_hash") == bh:
                return dict(prior.get("response") or response)
            raise ConflictError("idempotency key conflict") from exc

    return response


# Alias expected by RED contract probes.
resume_from_interrupt = apply_human_decision


def request_cancel(
    *,
    database_url: str,
    investigation_id: str | UUID,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Collaborative cancel — preserve public steps / evidence."""
    iid = str(investigation_id)

    if idempotency_key:
        prior = lookup_cancel_idempotency(
            database_url=database_url,
            investigation_id=iid,
            idempotency_key=idempotency_key,
        )
        if prior is not None:
            resp = dict(prior.get("response") or {})
            resp.setdefault("id", iid)
            return resp

    task = get_task(database_url=database_url, investigation_id=iid)
    if task is None:
        raise LookupError("not found")

    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    if task.get("task_status") in terminal:
        response = {
            "id": iid,
            "task_status": task["task_status"],
            "result_status": task.get("result_status"),
        }
    else:
        steps = list(task.get("public_steps") or [])
        steps.append({"kind": "cancelled", "reason": "user_cancel"})
        with _engine(database_url).begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE investigations
                    SET task_status = 'CANCELLED',
                        public_steps = CAST(:steps AS jsonb)
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": iid, "steps": json.dumps(steps, ensure_ascii=False)},
            )
        response = {"id": iid, "task_status": "CANCELLED"}

    if idempotency_key:
        remember_cancel_idempotency(
            database_url=database_url,
            investigation_id=iid,
            idempotency_key=idempotency_key,
            response=response,
        )
    return response


cancel_investigation = request_cancel


def _maybe_wake(*, database_url: str, investigation_id: str) -> None:
    try:
        from support_platform.config import load_settings

        settings = load_settings()
        redis_url = (settings.redis_url or "").strip()
        if not redis_url:
            return
        from support_platform.task_runtime.redis_adapter import RedisWakeAdapter

        RedisWakeAdapter(redis_url=redis_url).enqueue_wake(investigation_id=investigation_id)
    except Exception:  # noqa: BLE001
        return


class ConflictError(Exception):
    """Mapped to HTTP 409 by OP-10/11 routes."""
