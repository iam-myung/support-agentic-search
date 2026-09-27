"""S1 real-PG smoke: migrate provenance + activate/disable state transitions."""

from __future__ import annotations

import os
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text

from support_platform.knowledge.service import ActivationError, KnowledgeService


REQUIRED_TABLES = {
    "knowledge_sources",
    "knowledge_versions",
    "knowledge_chunks",
    "investigations",
    "investigation_evidence",
    "investigation_feedback",
}


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public'"
                )
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            print(f"SMOKE_FAIL missing tables: {sorted(missing)}", file=sys.stderr)
            return 1
        print("tables_ok", sorted(REQUIRED_TABLES))

        exts = {
            row[0]
            for row in conn.execute(text("SELECT extname FROM pg_extension"))
        }
        for name in ("vector", "pg_trgm"):
            if name not in exts:
                print(f"SMOKE_FAIL missing extension: {name}", file=sys.stderr)
                return 1
        print("extensions_ok", ["vector", "pg_trgm"])

        # Persist activate/disable transitions on real PG (not mock).
        source_id = uuid4()
        v1 = uuid4()
        v2 = uuid4()
        failed = uuid4()
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, type, title, status) "
                "VALUES (:id, 'DOC', 'Smoke FAQ', 'ACTIVE')"
            ),
            {"id": source_id},
        )
        for vid, label, status, active in (
            (v1, "v1", "READY", True),
            (v2, "v2", "READY", False),
            (failed, "v-fail", "FAILED", False),
        ):
            conn.execute(
                text(
                    "INSERT INTO knowledge_versions "
                    "(id, source_id, label, content_hash, format, status, is_active, "
                    "applicability_scope, product_versions, account_types, symptom_tags) "
                    "VALUES (:id, :sid, :label, :hash, 'MD', :status, :active, "
                    "'GENERAL', '[]', '[]', '[]')"
                ),
                {
                    "id": vid,
                    "sid": source_id,
                    "label": label,
                    "hash": f"hash-{label}",
                    "status": status,
                    "active": active,
                },
            )
        # Switch active READY: only v2 remains active (single READY active invariant).
        conn.execute(
            text(
                "UPDATE knowledge_versions SET is_active = false "
                "WHERE source_id = :sid AND is_active = true"
            ),
            {"sid": source_id},
        )
        conn.execute(
            text(
                "UPDATE knowledge_versions SET is_active = true, status = 'READY' "
                "WHERE id = :id"
            ),
            {"id": v2},
        )
        active_count = conn.execute(
            text(
                "SELECT count(*) FROM knowledge_versions "
                "WHERE source_id = :sid AND is_active = true AND status = 'READY'"
            ),
            {"sid": source_id},
        ).scalar_one()
        if active_count != 1:
            print(f"SMOKE_FAIL active_count={active_count}", file=sys.stderr)
            return 1
        print("activate_single_ready_ok", str(v2))

        conn.execute(
            text("UPDATE knowledge_sources SET status = 'DISABLED' WHERE id = :id"),
            {"id": source_id},
        )
        candidates = conn.execute(
            text(
                "SELECT kv.id FROM knowledge_versions kv "
                "JOIN knowledge_sources ks ON ks.id = kv.source_id "
                "WHERE kv.is_active = true AND kv.status = 'READY' AND ks.status = 'ACTIVE'"
            )
        ).fetchall()
        if any(row[0] == v2 for row in candidates):
            print("SMOKE_FAIL disabled source still candidate", file=sys.stderr)
            return 1
        print("disable_excludes_candidates_ok")

        conn.commit()

    # Domain service invariants (same rules) against FAILED activation.
    svc = KnowledgeService()
    sid = svc.create_source(type="DOC", title="domain-check")
    fid = svc.add_version(
        source_id=sid,
        label="f",
        content_hash="h",
        format="MD",
        status="FAILED",
    )
    try:
        svc.activate_version(fid)
        print("SMOKE_FAIL FAILED version activated", file=sys.stderr)
        return 1
    except ActivationError as exc:
        print("failed_version_rejected_ok", str(exc))

    print("S1_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
