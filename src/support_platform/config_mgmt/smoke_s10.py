"""S10 real-path SMOKE: OP-13 CLI + OP-00 --persist-snapshot → real PostgreSQL; drift-safe replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.investigation.pg_store import PgInvestigationStore
from support_platform.investigation.snapshot import get_frozen, replay_versions


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _cli(env: dict[str, str], *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "support_platform.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI {' '.join(args)} exit={proc.returncode} stderr={proc.stderr.strip()} stdout={proc.stdout.strip()}"
        )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def _seed_knowledge(database_url: str) -> tuple[str, str]:
    """Insert one READY active knowledge version; return (source_id, version_id)."""
    engine = create_engine(database_url, pool_pre_ping=True)
    source_id = str(uuid4())
    version_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_sources (id, type, title, status) "
                "VALUES (CAST(:id AS uuid), 'DOC', 'S10 Smoke FAQ', 'ACTIVE')"
            ),
            {"id": source_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_versions
                  (id, source_id, label, content_hash, format, status, is_active,
                   applicability_scope, product_versions, account_types, symptom_tags)
                VALUES
                  (CAST(:id AS uuid), CAST(:sid AS uuid), 's10-v1', 's10-know-v1', 'MD',
                   'READY', true, 'GENERAL', '[]', '[]', '[]')
                """
            ),
            {"id": version_id, "sid": source_id},
        )
    return source_id, version_id


def _activate_new_knowledge(database_url: str, source_id: str) -> str:
    engine = create_engine(database_url, pool_pre_ping=True)
    version_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE knowledge_versions SET is_active = false "
                "WHERE source_id = CAST(:sid AS uuid) AND is_active = true"
            ),
            {"sid": source_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_versions
                  (id, source_id, label, content_hash, format, status, is_active,
                   applicability_scope, product_versions, account_types, symptom_tags)
                VALUES
                  (CAST(:id AS uuid), CAST(:sid AS uuid), 's10-v2', 's10-know-v2', 'MD',
                   'READY', true, 'GENERAL', '[]', '[]', '[]')
                """
            ),
            {"id": version_id, "sid": source_id},
        )
    return version_id


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    print("official_entry_op13", "python -m support_platform.cli config register|activate|list")
    print("official_entry_op00", "python -m support_platform.cli investigate --question ... --persist-snapshot")
    print("database", database_url.split("@")[-1])
    print("no_fake", "PostgreSQL config_versions + investigations freeze columns")
    print("no_v2", "replay via OP-00 persist-snapshot + PgInvestigationStore.get")

    try:
        upgrade_head(database_url)
        print("migrate", "upgrade head ok (expects 003_config_versions)")
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL migrate", exc, file=sys.stderr)
        return 1

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            if "config_versions" not in tables:
                print("SMOKE_FAIL config_versions table missing", file=sys.stderr)
                return 1
            cols = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'investigations'"
                    )
                )
            }
            for required in (
                "knowledge_snapshot_hash",
                "prompt_version",
                "model_config_version",
                "retrieval_config_version",
            ):
                if required not in cols:
                    print(f"SMOKE_FAIL investigations.{required} missing", file=sys.stderr)
                    return 1
        print("tables_ok", ["config_versions", "investigations.freeze_columns"])
    except Exception as exc:  # noqa: BLE001
        print("NOT_EXECUTED: cannot connect to PostgreSQL:", exc, file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="s10_config_") as tmp:
        cfg_root = Path(tmp)
        prompt_v1 = cfg_root / "prompt_v1.md"
        prompt_v1.write_text("S10 freeze prompt v1\n", encoding="utf-8")
        prompt_v2 = cfg_root / "prompt_v2.md"
        prompt_v2.write_text("S10 NEW prompt must not drift old cases\n", encoding="utf-8")

        env = os.environ.copy()
        env["DATABASE_URL"] = database_url
        env["CONFIG_ROOT"] = str(cfg_root)
        env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")

        # Cleanup prior smoke config rows (kind-scoped ok to leave; use unique path content)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM config_versions WHERE created_by = 's10-smoke'"))

        source_id, know_v1 = _seed_knowledge(database_url)
        print("knowledge_seed", know_v1)

        try:
            p1 = _cli(
                env,
                "config",
                "register",
                "--kind",
                "prompt",
                "--path",
                str(prompt_v1),
                "--created-by",
                "s10-smoke",
            )
            m1 = _cli(
                env,
                "config",
                "register",
                "--kind",
                "model",
                "--payload",
                json.dumps({"model": "qwen-plus", "temperature": 0.0}),
                "--created-by",
                "s10-smoke",
            )
            r1 = _cli(
                env,
                "config",
                "register",
                "--kind",
                "retrieval",
                "--payload",
                json.dumps({"top_k": 10, "rrf_k": 60}),
                "--created-by",
                "s10-smoke",
            )
            for label, row in (("prompt", p1), ("model", m1), ("retrieval", r1)):
                _cli(env, "config", "activate", "--version-id", row["version_id"])
                print("op13_activate", label, row["version_id"][:8])

            listed = _cli(env, "config", "list")
            if not listed.get("ok") or len(listed.get("versions") or []) < 3:
                print("SMOKE_FAIL config list", listed, file=sys.stderr)
                return 1
            print("op13_list_ok", len(listed["versions"]))

            created = _cli(
                env,
                "investigate",
                "--question",
                "S10 smoke: how to reset MFA?",
                "--persist-snapshot",
            )
            if not created.get("ok") or not created.get("id"):
                print("SMOKE_FAIL investigate persist-snapshot", created, file=sys.stderr)
                return 1
            iid = created["id"]
            before = created["frozen"]
            print("op00_freeze_ok", iid, "prompt", str(before["prompt_version"])[:8])

            # Activate new config + knowledge (poison current actives)
            p2 = _cli(
                env,
                "config",
                "register",
                "--kind",
                "prompt",
                "--path",
                str(prompt_v2),
                "--created-by",
                "s10-smoke",
            )
            _cli(env, "config", "activate", "--version-id", p2["version_id"])
            m2 = _cli(
                env,
                "config",
                "register",
                "--kind",
                "model",
                "--payload",
                json.dumps({"model": "qwen-max", "temperature": 0.2}),
                "--created-by",
                "s10-smoke",
            )
            _cli(env, "config", "activate", "--version-id", m2["version_id"])
            know_v2 = _activate_new_knowledge(database_url, source_id)
            print("poison_actives", "prompt", p2["version_id"][:8], "know", know_v2[:8])

            store = PgInvestigationStore(database_url)
            after = get_frozen(store, iid)
            replay = replay_versions(store, iid)

            if after != before:
                print("SMOKE_FAIL drift after activate", {"before": before, "after": after}, file=sys.stderr)
                return 1
            if str(after["prompt_version"]) == str(p2["version_id"]):
                print("SMOKE_FAIL frozen prompt equals new active", file=sys.stderr)
                return 1
            if know_v1 not in [str(x) for x in after["active_version_ids"]]:
                print("SMOKE_FAIL frozen knowledge missing v1", after, file=sys.stderr)
                return 1
            if know_v2 in [str(x) for x in after["active_version_ids"]]:
                print("SMOKE_FAIL frozen knowledge picked up v2", after, file=sys.stderr)
                return 1
            if replay["config_hash"] != before["config_hash"]:
                print("SMOKE_FAIL replay config_hash drifted", file=sys.stderr)
                return 1

            print("replay_ok", "frozen_prompt", str(replay["prompt_version"])[:8])
            print("no_drift", "config+knowledge activate did not change investigation", iid)
            print("S10_SMOKE_OK")
            return 0
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL", exc, file=sys.stderr)
            return 1
        finally:
            # Cleanup smoke rows
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM investigations WHERE question LIKE 'S10 smoke:%'")
                )
                conn.execute(text("DELETE FROM config_versions WHERE created_by = 's10-smoke'"))
                conn.execute(
                    text(
                        "DELETE FROM knowledge_versions WHERE source_id IN "
                        "(SELECT id FROM knowledge_sources WHERE title = 'S10 Smoke FAQ')"
                    )
                )
                conn.execute(
                    text("DELETE FROM knowledge_sources WHERE title = 'S10 Smoke FAQ'")
                )
            print("cleanup_ok")


if __name__ == "__main__":
    raise SystemExit(main())
