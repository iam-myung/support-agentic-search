"""S10 investigation knowledge/config snapshot drift RED (REQ-010 / AC-012).

Focus: freeze on create; activate new config/knowledge must not alter old investigation.
Replay uses frozen versions — no v2 API required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest


def _config_service_cls():
    try:
        from support_platform.config_mgmt.service import ConfigVersionService

        return ConfigVersionService
    except ImportError as exc:
        pytest.fail(f"S10 ConfigVersionService missing: {exc}")


def _snapshot_api():
    """Public freeze/read helpers for investigation creation (S10)."""
    try:
        from support_platform.investigation import snapshot as snap_mod

        return snap_mod
    except ImportError as exc:
        pytest.fail(f"S10 investigation.snapshot module missing: {exc}")


def _seed_active_configs(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "cfg"
    root.mkdir()
    prompt = root / "prompt_v1.md"
    prompt.write_text("Investigate with citations only.\n", encoding="utf-8")

    ConfigVersionService = _config_service_cls()
    svc = ConfigVersionService(config_root=root)
    prompt_v = svc.register(kind="prompt", path=prompt, created_by="ops@local")
    model_v = svc.register(
        kind="model",
        payload={"model": "qwen-plus", "temperature": 0.0},
        created_by="ops@local",
    )
    retrieval_v = svc.register(
        kind="retrieval",
        payload={"top_k": 10, "rrf_k": 60},
        created_by="ops@local",
    )
    svc.activate(prompt_v["version_id"])
    svc.activate(model_v["version_id"])
    svc.activate(retrieval_v["version_id"])
    return {
        "svc": svc,
        "root": root,
        "prompt_version": str(prompt_v["version_id"]),
        "model_config_version": str(model_v["version_id"]),
        "retrieval_config_version": str(retrieval_v["version_id"]),
        "prompt_hash": prompt_v["content_hash"],
        "model_hash": model_v["content_hash"],
        "retrieval_hash": retrieval_v["content_hash"],
    }


def _seed_knowledge_versions() -> dict[str, Any]:
    from support_platform.knowledge.service import KnowledgeService

    ks = KnowledgeService()
    source_id = ks.create_source(type="DOC", title="FAQ-S10")
    v1 = ks.add_version(
        source_id=source_id,
        label="kv1",
        content_hash="know-hash-v1",
        format="MD",
        status="READY",
    )
    ks.activate_version(v1)
    return {"knowledge": ks, "source_id": source_id, "version_v1": str(v1)}


def test_investigations_metadata_has_snapshot_version_fields() -> None:
    from support_platform.infrastructure.db.models import metadata

    assert "investigations" in metadata.tables
    cols = {c.name for c in metadata.tables["investigations"].columns}
    for required in (
        "active_version_ids",
        "config_hash",
        "knowledge_snapshot_hash",
        "prompt_version",
        "model_config_version",
        "retrieval_config_version",
    ):
        assert required in cols, f"investigations missing freeze column: {required}"


def test_create_investigation_freezes_knowledge_and_config_versions(
    tmp_path: Path,
) -> None:
    cfg = _seed_active_configs(tmp_path)
    know = _seed_knowledge_versions()
    snap = _snapshot_api()

    frozen = snap.freeze_for_create(
        knowledge=know["knowledge"],
        config=cfg["svc"],
    )
    assert frozen["active_version_ids"] == [know["version_v1"]] or set(
        frozen["active_version_ids"]
    ) == {know["version_v1"]}
    assert str(frozen["prompt_version"]) == cfg["prompt_version"]
    assert str(frozen["model_config_version"]) == cfg["model_config_version"]
    assert str(frozen["retrieval_config_version"]) == cfg["retrieval_config_version"]
    assert frozen.get("knowledge_snapshot_hash")
    assert frozen.get("config_hash")
    # Combined config hash must cover all three kinds.
    assert len(str(frozen["config_hash"])) >= 16


def test_activate_new_config_does_not_drift_existing_investigation(
    tmp_path: Path,
) -> None:
    """Core S10 RED: same investigation keeps frozen versions after activate."""
    cfg = _seed_active_configs(tmp_path)
    know = _seed_knowledge_versions()
    snap = _snapshot_api()

    created = snap.create_with_snapshot(
        question="How do I reset MFA?",
        knowledge=know["knowledge"],
        config=cfg["svc"],
    )
    iid = created["id"]
    before = snap.get_frozen(created["store"], iid)

    # Register + activate a brand-new config set (would poison naive "always read active").
    new_prompt = cfg["root"] / "prompt_v2.md"
    new_prompt.write_text("NEW PROMPT must not affect old cases.\n", encoding="utf-8")
    v_new = cfg["svc"].register(kind="prompt", path=new_prompt, created_by="ops@local")
    cfg["svc"].activate(v_new["version_id"])
    cfg["svc"].register(
        kind="model",
        payload={"model": "qwen-max", "temperature": 0.2},
        created_by="ops@local",
    )
    # activate newest model
    models = cfg["svc"].list_versions(kind="model")
    newest_model = models[-1]["version_id"]
    cfg["svc"].activate(newest_model)

    after = snap.get_frozen(created["store"], iid)
    assert after["prompt_version"] == before["prompt_version"]
    assert after["model_config_version"] == before["model_config_version"]
    assert after["retrieval_config_version"] == before["retrieval_config_version"]
    assert after["config_hash"] == before["config_hash"]
    assert after["knowledge_snapshot_hash"] == before["knowledge_snapshot_hash"]
    assert after["active_version_ids"] == before["active_version_ids"]
    # And frozen prompt is NOT the newly activated one.
    assert str(after["prompt_version"]) != str(v_new["version_id"])


def test_activate_new_knowledge_does_not_drift_existing_investigation(
    tmp_path: Path,
) -> None:
    cfg = _seed_active_configs(tmp_path)
    know = _seed_knowledge_versions()
    snap = _snapshot_api()
    ks = know["knowledge"]

    created = snap.create_with_snapshot(
        question="Password reset steps?",
        knowledge=ks,
        config=cfg["svc"],
    )
    iid = created["id"]
    before = snap.get_frozen(created["store"], iid)

    v2 = ks.add_version(
        source_id=know["source_id"],
        label="kv2",
        content_hash="know-hash-v2",
        format="MD",
        status="READY",
    )
    ks.activate_version(v2)

    after = snap.get_frozen(created["store"], iid)
    assert after["active_version_ids"] == before["active_version_ids"]
    assert know["version_v1"] in [str(x) for x in after["active_version_ids"]]
    assert str(v2) not in [str(x) for x in after["active_version_ids"]]
    assert after["knowledge_snapshot_hash"] == before["knowledge_snapshot_hash"]


def test_replay_reads_frozen_snapshot_not_current_active(tmp_path: Path) -> None:
    """OP-00 style replay: load by investigation id; no v2 API."""
    cfg = _seed_active_configs(tmp_path)
    know = _seed_knowledge_versions()
    snap = _snapshot_api()

    created = snap.create_with_snapshot(
        question="Replay probe",
        knowledge=know["knowledge"],
        config=cfg["svc"],
    )
    iid = created["id"]
    frozen_ids = list(snap.get_frozen(created["store"], iid)["active_version_ids"])

    # Poison current actives
    v2 = know["knowledge"].add_version(
        source_id=know["source_id"],
        label="kv-poison",
        content_hash="know-poison",
        format="MD",
        status="READY",
    )
    know["knowledge"].activate_version(v2)
    new_prompt = cfg["root"] / "poison.md"
    new_prompt.write_text("poison\n", encoding="utf-8")
    p2 = cfg["svc"].register(kind="prompt", path=new_prompt, created_by="ops@local")
    cfg["svc"].activate(p2["version_id"])

    replay = snap.replay_versions(created["store"], iid)
    assert [str(x) for x in replay["active_version_ids"]] == [str(x) for x in frozen_ids]
    assert str(replay["prompt_version"]) != str(p2["version_id"])
    # UUID-shaped version ids
    UUID(str(replay["prompt_version"]))
    UUID(str(replay["model_config_version"]))
    UUID(str(replay["retrieval_config_version"]))
