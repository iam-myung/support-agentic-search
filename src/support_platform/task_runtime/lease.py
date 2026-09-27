"""Task lease claim and PostgreSQL backlog scan (SPEC §3/§6) — PG is task truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def scan_due_tasks(*, database_url: str) -> list[dict[str, Any]]:
    """List claimable tasks from PostgreSQL (works when Redis wake queue is empty/lost)."""
    with _engine(database_url).connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.id::text AS investigation_id, i.task_status,
                       l.worker_id, l.attempt, l.leased_until
                FROM investigations i
                LEFT JOIN LATERAL (
                  SELECT worker_id, attempt, leased_until
                  FROM task_leases
                  WHERE investigation_id = i.id
                  ORDER BY attempt DESC NULLS LAST
                  LIMIT 1
                ) l ON true
                WHERE i.task_status = 'QUEUED'
                   OR (
                     i.task_status = 'RUNNING'
                     AND (l.leased_until IS NULL OR l.leased_until < NOW())
                   )
                ORDER BY i.created_at DESC
                """
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def list_claimable(*, database_url: str) -> list[dict[str, Any]]:
    return scan_due_tasks(database_url=database_url)


def claim_next(
    *,
    database_url: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> dict[str, Any] | None:
    """Atomically claim one due task; exclusive while lease is active."""
    until = datetime.now(timezone.utc) + timedelta(seconds=int(lease_seconds))
    with _engine(database_url).begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT i.id::text AS investigation_id
                FROM investigations i
                LEFT JOIN LATERAL (
                  SELECT leased_until, attempt
                  FROM task_leases
                  WHERE investigation_id = i.id
                  ORDER BY attempt DESC NULLS LAST
                  LIMIT 1
                ) l ON true
                WHERE i.task_status = 'QUEUED'
                   OR (
                     i.task_status = 'RUNNING'
                     AND (l.leased_until IS NULL OR l.leased_until < NOW())
                   )
                ORDER BY i.created_at DESC
                LIMIT 1
                FOR UPDATE OF i SKIP LOCKED
                """
            )
        ).mappings().first()
        if row is None:
            return None
        iid = row["investigation_id"]
        prev = conn.execute(
            text(
                """
                SELECT attempt FROM task_leases
                WHERE investigation_id = CAST(:iid AS uuid)
                ORDER BY attempt DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"iid": iid},
        ).scalar()
        attempt = int(prev or 0) + 1
        conn.execute(
            text(
                """
                INSERT INTO task_leases (id, investigation_id, worker_id, leased_until, attempt)
                VALUES (
                  CAST(:lid AS uuid), CAST(:iid AS uuid), :wid, :until, :attempt
                )
                """
            ),
            {
                "lid": str(uuid4()),
                "iid": iid,
                "wid": worker_id,
                "until": until,
                "attempt": attempt,
            },
        )
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'RUNNING'
                WHERE id = CAST(:iid AS uuid)
                """
            ),
            {"iid": iid},
        )
    return {
        "investigation_id": iid,
        "worker_id": worker_id,
        "leased_until": until.isoformat(),
        "attempt": attempt,
        "task_status": "RUNNING",
    }


def claim_lease(
    *,
    database_url: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> dict[str, Any] | None:
    return claim_next(
        database_url=database_url,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )


def force_expire_lease(
    *,
    database_url: str,
    investigation_id: UUID | str,
    leased_until: datetime,
) -> None:
    """Test/ops helper: mark the latest lease as expired so another worker may reclaim."""
    with _engine(database_url).begin() as conn:
        conn.execute(
            text(
                """
                UPDATE task_leases
                SET leased_until = :until
                WHERE id = (
                  SELECT id FROM task_leases
                  WHERE investigation_id = CAST(:iid AS uuid)
                  ORDER BY attempt DESC NULLS LAST
                  LIMIT 1
                )
                """
            ),
            {"iid": str(investigation_id), "until": leased_until},
        )


def debug_set_leased_until(
    *,
    database_url: str,
    investigation_id: UUID | str,
    leased_until: datetime,
) -> None:
    force_expire_lease(
        database_url=database_url,
        investigation_id=investigation_id,
        leased_until=leased_until,
    )
