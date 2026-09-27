"""S1 schema contract: Phase 1 tables must exist after empty-DB migration metadata."""

from __future__ import annotations

PHASE1_REQUIRED_TABLES = frozenset(
    {
        "knowledge_sources",
        "knowledge_versions",
        "knowledge_chunks",
        "investigations",
        "investigation_evidence",
        "investigation_feedback",
    }
)


def test_phase1_sqlalchemy_metadata_includes_required_tables() -> None:
    from support_platform.infrastructure.db.models import metadata

    present = set(metadata.tables.keys())
    missing = PHASE1_REQUIRED_TABLES - present
    assert not missing, f"Phase 1 metadata missing tables: {sorted(missing)}"


def test_upgrade_head_is_wired_for_alembic() -> None:
    from support_platform.infrastructure.db.migrate import upgrade_head

    # GREEN must provide a real Alembic upgrade_head; RED stub leaves this False.
    assert getattr(upgrade_head, "is_implemented", False) is True
