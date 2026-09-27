"""S10 config_versions + OP-13 RED contracts (REQ-010 / AC-012).

Focus: immutable register, atomic activate, CONFIG_ROOT path gate, no secrets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SECRET_KEYS = frozenset(
    {
        "api_key",
        "API_KEY",
        "llm_api_key",
        "embedding_api_key",
        "password",
        "secret",
        "token",
    }
)


def _config_service_cls():
    """Load ConfigVersionService or fail as assertion (not collection ImportError)."""
    try:
        from support_platform.config_mgmt.service import ConfigVersionService

        return ConfigVersionService
    except ImportError as exc:
        pytest.fail(f"S10 ConfigVersionService missing: {exc}")


def test_metadata_includes_config_versions_table() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "config_versions" in metadata.tables, (
        "SPEC §3 requires config_versions for Prompt/model/retrieval versions"
    )


def test_config_versions_table_has_required_columns() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "config_versions" in metadata.tables
    cols = {c.name for c in metadata.tables["config_versions"].columns}
    for required in (
        "id",
        "kind",
        "content_hash",
        "payload",
        "is_active",
        "created_by",
        "created_at",
    ):
        assert required in cols, f"config_versions missing column: {required}"
    # Secrets must never be first-class columns.
    secret_cols = cols & {k.lower() for k in SECRET_KEYS} | (cols & SECRET_KEYS)
    assert not secret_cols, f"config_versions must not store secret columns: {secret_cols}"


def test_alembic_config_versions_revision_exists() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    names = [p.name for p in versions.glob("*.py") if p.name != "__init__.py"]
    assert any(
        ("003" in n) or ("config_version" in n.lower()) for n in names
    ), f"expected config_versions Alembic revision (003); found={names}"


def test_settings_exposes_config_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/support")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("CONFIG_ROOT", str(Path.cwd() / "config_templates"))

    from support_platform.config import load_settings

    settings = load_settings()
    assert hasattr(settings, "config_root"), "Settings must expose config_root (SPEC §6 CONFIG_ROOT)"
    assert str(settings.config_root).rstrip("/\\").endswith("config_templates")


def test_cli_exposes_op13_config_register_activate_list() -> None:
    from support_platform.cli import build_parser

    parser = build_parser()
    # OP-13: config register / activate / list
    try:
        ns = parser.parse_args(["config", "list"])
    except SystemExit as exc:
        pytest.fail(f"OP-13 config list subcommand missing: {exc}")
    assert getattr(ns, "command", None) == "config" or getattr(ns, "config_command", None) == "list"

    try:
        parser.parse_args(["config", "register", "--kind", "prompt", "--path", "x.md"])
    except SystemExit as exc:
        pytest.fail(f"OP-13 config register subcommand missing: {exc}")

    try:
        parser.parse_args(["config", "activate", "--version-id", "00000000-0000-0000-0000-000000000001"])
    except SystemExit as exc:
        pytest.fail(f"OP-13 config activate subcommand missing: {exc}")


def test_register_prompt_returns_version_id_and_content_hash(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    root.mkdir()
    prompt_file = root / "system.md"
    prompt_file.write_text("You are a support investigator.\n", encoding="utf-8")

    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)
    result = svc.register(
        kind="prompt",
        path=prompt_file,
        created_by="ops@local",
    )
    assert "version_id" in result and result["version_id"]
    assert "content_hash" in result and len(str(result["content_hash"])) >= 16
    listed = svc.list_versions(kind="prompt")
    assert any(str(row["version_id"]) == str(result["version_id"]) for row in listed)


def test_register_rejects_path_outside_config_root(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("leak", encoding="utf-8")

    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)
    with pytest.raises(Exception) as exc_info:
        svc.register(kind="prompt", path=outside, created_by="ops@local")
    msg = str(exc_info.value).lower()
    assert "config_root" in msg or "allow" in msg or "path" in msg or "outside" in msg


def test_register_rejects_secret_payload_fields(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    root.mkdir()

    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)
    with pytest.raises(Exception) as exc_info:
        svc.register(
            kind="model",
            payload={
                "model": "qwen-plus",
                "temperature": 0.0,
                "api_key": "sk-should-never-persist",
            },
            created_by="ops@local",
        )
    msg = str(exc_info.value).lower()
    assert "secret" in msg or "api_key" in msg or "key" in msg


def test_activate_switches_active_version_for_kind(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    root.mkdir()
    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)

    v1 = svc.register(
        kind="retrieval",
        payload={"top_k": 10, "rrf_k": 60},
        created_by="ops@local",
    )
    v2 = svc.register(
        kind="retrieval",
        payload={"top_k": 5, "rrf_k": 60},
        created_by="ops@local",
    )
    svc.activate(v1["version_id"])
    svc.activate(v2["version_id"])

    active = svc.get_active(kind="retrieval")
    assert active is not None
    assert str(active["version_id"]) == str(v2["version_id"])
    # Prior version remains listed but not active.
    rows = svc.list_versions(kind="retrieval")
    v1_row = next(r for r in rows if str(r["version_id"]) == str(v1["version_id"]))
    assert v1_row.get("is_active") is False


def test_referenced_config_version_cannot_be_deleted(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    root.mkdir()
    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)
    registered = svc.register(
        kind="model",
        payload={"model": "qwen-plus", "temperature": 0.0},
        created_by="ops@local",
    )
    vid = registered["version_id"]
    svc.activate(vid)
    svc.mark_referenced(vid, investigation_id="00000000-0000-0000-0000-0000000000aa")

    with pytest.raises(Exception) as exc_info:
        svc.delete(vid)
    msg = str(exc_info.value).lower()
    assert "referen" in msg or "in use" in msg or "cannot" in msg or "forbidden" in msg
