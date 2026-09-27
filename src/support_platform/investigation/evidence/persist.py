"""Persist claim-evidence snapshot rows into investigation_evidence (Phase 1)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text


def persist_evidence_rows(
    *,
    database_url: str,
    investigation_id: str,
    evidence: list[dict[str, Any]],
    claim_index: int = 0,
) -> list[str]:
    """Insert frozen evidence snapshots; chunk_id must exist in knowledge_chunks."""
    engine = create_engine(database_url, pool_pre_ping=True)
    inserted: list[str] = []
    with engine.begin() as conn:
        for row in evidence:
            eid = str(row.get("id") or uuid4())
            chunk_id = str(row["chunk_id"])
            quote = str(row.get("quote_snapshot") or row.get("quote") or "")
            locator = row.get("locator_snapshot") or row.get("locator") or {"kind": "md", "line_start": 1, "line_end": 1}
            title = str(row.get("source_title_snapshot") or row.get("source_title") or "unknown")
            version = row.get("version_snapshot") or {"label": "v1"}
            if not isinstance(version, (dict, list)):
                version = {"id": str(version)}
            conn.execute(
                text(
                    """
                    INSERT INTO investigation_evidence (
                      id, investigation_id, claim_index, chunk_id, relation,
                      quote_snapshot, locator_snapshot, source_title_snapshot, version_snapshot
                    ) VALUES (
                      CAST(:id AS uuid), CAST(:iid AS uuid), :ci, CAST(:cid AS uuid), :rel,
                      :quote, CAST(:loc AS jsonb), :title, CAST(:ver AS jsonb)
                    )
                    """
                ),
                {
                    "id": eid,
                    "iid": investigation_id,
                    "ci": claim_index,
                    "cid": chunk_id,
                    "rel": str(row.get("relation") or "SUPPORTS"),
                    "quote": quote,
                    "loc": json.dumps(locator, ensure_ascii=False),
                    "title": title[:512],
                    "ver": json.dumps(version, ensure_ascii=False),
                },
            )
            inserted.append(eid)
    return inserted
