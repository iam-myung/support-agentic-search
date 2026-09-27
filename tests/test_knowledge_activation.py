"""S1 knowledge invariants: single READY active; disable; FAILED cannot activate."""

from __future__ import annotations

from uuid import UUID

import pytest


def _new_service():
    from support_platform.knowledge.service import KnowledgeService

    return KnowledgeService()


def test_at_most_one_ready_active_version_per_source() -> None:
    svc = _new_service()
    source_id = svc.create_source(type="DOC", title="FAQ")
    v1 = svc.add_version(
        source_id=source_id,
        label="v1",
        content_hash="hash-a",
        format="MD",
        status="READY",
    )
    v2 = svc.add_version(
        source_id=source_id,
        label="v2",
        content_hash="hash-b",
        format="MD",
        status="READY",
    )
    svc.activate_version(v1)
    svc.activate_version(v2)

    active = svc.list_active_ready_versions(source_id=source_id)
    assert len(active) == 1
    assert active[0] == v2


def test_disabled_source_excluded_from_investigation_candidates() -> None:
    svc = _new_service()
    source_id = svc.create_source(type="DOC", title="Guide")
    version_id = svc.add_version(
        source_id=source_id,
        label="v1",
        content_hash="hash-c",
        format="MD",
        status="READY",
    )
    svc.activate_version(version_id)
    svc.disable_source(source_id)

    candidates = svc.list_investigation_candidates()
    assert version_id not in candidates
    assert source_id not in {c.source_id for c in svc.list_candidate_records()}


def test_failed_version_cannot_be_activated() -> None:
    svc = _new_service()
    source_id = svc.create_source(type="DOC", title="Broken")
    failed_id = svc.add_version(
        source_id=source_id,
        label="v-fail",
        content_hash="hash-d",
        format="MD",
        status="FAILED",
    )

    with pytest.raises(Exception) as exc_info:
        svc.activate_version(failed_id)

    assert "FAILED" in str(exc_info.value).upper() or "activ" in str(exc_info.value).lower()
    assert svc.list_active_ready_versions(source_id=source_id) == []


def test_empty_content_version_cannot_be_activated() -> None:
    svc = _new_service()
    source_id = svc.create_source(type="DOC", title="Empty")
    empty_id = svc.add_version(
        source_id=source_id,
        label="v-empty",
        content_hash="hash-e",
        format="MD",
        status="READY",
        is_empty=True,
    )

    with pytest.raises(Exception):
        svc.activate_version(empty_id)

    assert svc.list_active_ready_versions(source_id=source_id) == []


def test_create_source_returns_uuid() -> None:
    svc = _new_service()
    source_id = svc.create_source(
        type="CASE",
        title="Case-1",
        case_ref="C-1",
        confirmed_resolution=True,
    )
    assert source_id is not None
    UUID(str(source_id))
