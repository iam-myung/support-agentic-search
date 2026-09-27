"""S9 Phase 2 schema RED: users/sessions + investigation ownership after empty-DB migrate metadata."""

from __future__ import annotations

from pathlib import Path

PHASE2_AUTH_TABLES = frozenset({"users", "sessions"})


def test_phase2_sqlalchemy_metadata_includes_users_and_sessions() -> None:
    from support_platform.infrastructure.db.models import metadata

    present = set(metadata.tables.keys())
    missing = PHASE2_AUTH_TABLES - present
    assert not missing, f"Phase 2 auth metadata missing tables: {sorted(missing)}"


def test_investigations_metadata_has_owner_user_id() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "investigations" in metadata.tables
    cols = {c.name for c in metadata.tables["investigations"].columns}
    assert "owner_user_id" in cols, "investigations must carry owner_user_id for visibility (SPEC §3/§4)"


def test_users_table_has_role_and_credentials_columns() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "users" in metadata.tables, "users table missing from metadata"
    cols = {c.name for c in metadata.tables["users"].columns}
    for required in ("id", "username", "password_hash", "role", "is_active"):
        assert required in cols, f"users missing column: {required}"


def test_sessions_table_has_token_and_user_fk_columns() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "sessions" in metadata.tables, "sessions table missing from metadata"
    cols = {c.name for c in metadata.tables["sessions"].columns}
    for required in ("id", "user_id", "token_hash", "expires_at", "csrf_token"):
        assert required in cols, f"sessions missing column: {required}"


def test_phase2_alembic_revision_file_exists() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    names = [p.name for p in versions.glob("*.py") if p.name != "__init__.py"]
    assert any(
        ("002" in n) or ("phase2" in n.lower()) for n in names
    ), f"expected Phase 2 Alembic revision (002/phase2); found={names}"
