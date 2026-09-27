"""S12 Redis worker / lease / PG-scan RED (REQ-007 / AC-009 remainder).

Focus (SPEC §2/§3/§6/§8 S12):
- duplicate delivery must not produce a second final output
- Redis loss must not drop accepted tasks — PG backlog scan recovers
- expired lease is reclaimable via PG
Redis must never be the sole source of truth.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest


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
    raise AssertionError("DATABASE_URL must be set for S12 worker/lease contracts")


def _load_redis_url() -> str:
    url = os.environ.get("REDIS_URL", "").strip()
    if url:
        return url
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "REDIS_URL":
                return value.strip().strip('"').strip("'")
    # Allow empty for "Redis down" assertions; GREEN will document compose default.
    return url


def _worker_api():
    try:
        from support_platform.task_runtime import worker as w

        return w
    except ImportError as exc:
        raise AssertionError(
            "support_platform.task_runtime.worker must exist (S12 worker ownership)"
        ) from exc


def _lease_api():
    try:
        from support_platform.task_runtime import lease as lease_mod

        return lease_mod
    except ImportError as exc:
        raise AssertionError(
            "support_platform.task_runtime.lease must exist (task_leases claim/scan)"
        ) from exc


def _redis_adapter_cls():
    try:
        from support_platform.task_runtime.redis_adapter import RedisWakeAdapter

        return RedisWakeAdapter
    except ImportError as exc:
        raise AssertionError(
            "RedisWakeAdapter missing under task_runtime.redis_adapter"
        ) from exc


def _seed_queued(*, database_url: str, question: str = "S12-RED queued"):
    from support_platform.task_runtime import create_queued_investigation

    iid = uuid4()
    create_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question=question,
        context={},
        owner_user_id=uuid4(),
        knowledge_version_ids=[],
        config_versions={},
    )
    return iid


# --- Settings / entrypoints ---


def test_settings_exposes_redis_url_and_worker_lease_seconds() -> None:
    from support_platform.config import Settings

    fields = Settings.model_fields
    assert "redis_url" in fields, "Settings must expose redis_url (REDIS_URL)"
    assert "worker_lease_seconds" in fields, (
        "Settings must expose worker_lease_seconds (WORKER_LEASE_SECONDS)"
    )


def test_worker_module_entrypoint_exists() -> None:
    """Official worker entry: python -m support_platform.worker (or package __main__)."""
    try:
        import support_platform.worker as worker_mod
    except ImportError as exc:
        raise AssertionError(
            "support_platform.worker entry module missing (SPEC §2 web/worker split)"
        ) from exc
    assert callable(getattr(worker_mod, "main", None)) or hasattr(worker_mod, "__main__") or hasattr(
        worker_mod, "run_once"
    ), "worker module must expose main/run_once"


# --- Lease claim / duplicate delivery ---


def test_claim_lease_is_exclusive_for_queued_task() -> None:
    lease = _lease_api()
    database_url = _load_database_url()
    iid = _seed_queued(database_url=database_url, question="S12-RED exclusive lease")

    claim = getattr(lease, "claim_next", None) or getattr(lease, "claim_lease", None)
    assert callable(claim), "lease.claim_next/claim_lease required"

    first = claim(
        database_url=database_url,
        worker_id="worker-a",
        lease_seconds=60,
    )
    second = claim(
        database_url=database_url,
        worker_id="worker-b",
        lease_seconds=60,
    )
    # First claim must get this (or some) QUEUED task; second must not get the same id.
    assert first is not None and str(first.get("investigation_id")) == str(iid), (
        f"first worker must claim seeded QUEUED task; got {first}"
    )
    if second is not None:
        assert str(second.get("investigation_id")) != str(iid), (
            "second worker must not claim the same leased investigation"
        )


def test_duplicate_delivery_does_not_create_second_final() -> None:
    """Two process attempts for the same task must still yield exactly one final output."""
    worker = _worker_api()
    database_url = _load_database_url()
    iid = _seed_queued(database_url=database_url, question="S12-RED duplicate delivery")

    process = getattr(worker, "process_investigation", None) or getattr(
        worker, "run_one", None
    )
    assert callable(process), "worker.process_investigation/run_one required"

    # Deterministic stub path: no paid model required for RED contract.
    r1 = process(
        database_url=database_url,
        investigation_id=iid,
        worker_id="w1",
        mode="deterministic_final",
    )
    r2 = process(
        database_url=database_url,
        investigation_id=iid,
        worker_id="w2",
        mode="deterministic_final",
    )
    assert r1.get("task_status") == "COMPLETED" or r1.get("accepted") is True
    assert r2.get("duplicate") is True or r2.get("accepted") is False or r2.get(
        "task_status"
    ) == "COMPLETED"

    from support_platform.task_runtime import count_final_outputs

    assert count_final_outputs(database_url=database_url, investigation_id=iid) == 1


# --- Redis loss → PG scan ---


def test_redis_wake_adapter_degrades_when_redis_unavailable() -> None:
    Adapter = _redis_adapter_cls()
    adapter = Adapter(redis_url="redis://127.0.0.1:1/0")  # closed port
    # Must not raise as fatal for task truth; report degraded / not ready.
    status = adapter.health()
    assert status.get("ok") is False or status.get("degraded") is True, (
        f"Redis down must be degraded/not-ok, not silent ok: {status}"
    )
    # Wake failure must not erase PG tasks — enqueue_wake returns False/None safely.
    woke = adapter.enqueue_wake(investigation_id=str(uuid4()))
    assert woke is False or woke is None


def test_pg_backlog_scan_recovers_queued_when_redis_flushed() -> None:
    lease = _lease_api()
    Adapter = _redis_adapter_cls()
    database_url = _load_database_url()
    redis_url = _load_redis_url() or "redis://127.0.0.1:6379/15"
    iid = _seed_queued(database_url=database_url, question="S12-RED redis flush recover")

    adapter = Adapter(redis_url=redis_url)
    try:
        adapter.enqueue_wake(investigation_id=str(iid))
        adapter.flush_wakeup_queue()  # simulate Redis loss of wake keys
    except Exception:
        # Redis may be down entirely — still must recover via PG scan.
        pass

    scan = getattr(lease, "scan_due_tasks", None) or getattr(lease, "list_claimable", None)
    assert callable(scan), "lease.scan_due_tasks/list_claimable required for PG fallback"
    due = scan(database_url=database_url)
    ids = {str(item.get("investigation_id") or item.get("id")) for item in due}
    assert str(iid) in ids, (
        f"accepted QUEUED task must remain discoverable via PG scan after Redis loss; due={due}"
    )


# --- Lease timeout reclaim / unsafe resume ---


def test_expired_lease_is_reclaimable_via_pg() -> None:
    lease = _lease_api()
    database_url = _load_database_url()
    iid = _seed_queued(database_url=database_url, question="S12-RED lease expiry")

    claim = getattr(lease, "claim_next", None) or getattr(lease, "claim_lease", None)
    force_expire = getattr(lease, "force_expire_lease", None) or getattr(
        lease, "debug_set_leased_until", None
    )
    assert callable(claim) and callable(force_expire)

    first = claim(database_url=database_url, worker_id="worker-expire-a", lease_seconds=60)
    assert first is not None and str(first["investigation_id"]) == str(iid)

    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    force_expire(database_url=database_url, investigation_id=iid, leased_until=past)

    second = claim(database_url=database_url, worker_id="worker-expire-b", lease_seconds=60)
    assert second is not None and str(second["investigation_id"]) == str(iid), (
        f"expired lease must be reclaimable; got {second}"
    )
    assert second.get("worker_id") == "worker-expire-b" or second.get("attempt", 0) >= 1


def test_unsafe_resume_marks_failed_with_explicit_status() -> None:
    """Cannot safely continue → FAILED (not stuck RUNNING forever). AC-009 remainder."""
    worker = _worker_api()
    database_url = _load_database_url()
    iid = _seed_queued(database_url=database_url, question="S12-RED unsafe resume")

    mark_unsafe = getattr(worker, "mark_unsafe_resume_failed", None) or getattr(
        worker, "fail_unsafe_resume", None
    )
    assert callable(mark_unsafe), "worker must expose unsafe-resume → FAILED helper"

    out = mark_unsafe(
        database_url=database_url,
        investigation_id=iid,
        error_code="UNSAFE_RESUME",
        public_steps=[{"step": "lease_expired_no_checkpoint"}],
    )
    assert out.get("task_status") == "FAILED"
    assert out.get("error_code") == "UNSAFE_RESUME"
    assert out.get("result_status") in (None, "NULL") or out.get("result_status") is None

    from support_platform.task_runtime import get_task

    task = get_task(database_url=database_url, investigation_id=iid)
    assert task is not None
    assert task["task_status"] == "FAILED"
    assert task.get("public_steps"), "public steps must be retained on unsafe-resume failure"
