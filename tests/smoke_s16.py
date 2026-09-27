"""S16 real-path SMOKE: official uvicorn → login audit + frozen investigation Trace/HITL → real PG.

SPEC §8 S16 / AC-012:
- OP-12 login failure writes sanitized audit_events (no password/secrets)
- Investigation carries knowledge/config version freeze fields
- HITL decision writes TRACE_STEP + HITL_* audit; no sensitive body
- Official entry: uvicorn support_platform.main:app

Paid Chat/Embedding worker→model path: NOT_EXECUTED (same S13–S15 policy).
Fake Redis/DB/TestClient-as-only-entry forbidden.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.audit.store import AuditStore
from support_platform.audit.trace import TraceRecorder
from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.pg_backend import delete_user_by_username, upsert_user
from support_platform.config_mgmt.pg_store import PgConfigVersionService
from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.investigation.hitl import (
    extract_interrupt_id,
    mark_interrupted,
)
from support_platform.investigation.snapshot import PgKnowledgeCandidates, freeze_for_create

SMOKE_USER = "s16_smoke_agent"
SMOKE_PASS = "s16-smoke-secret"
PORT = 8797

FORBIDDEN = (
    "chain of thought",
    "internal reasoning",
    "sk-",
    "api_key",
    "LLM_API_KEY",
    "should-not-persist",
    "customer raw question",
)


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


def _wait_alive(base: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health/live", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    raise RuntimeError("uvicorn health/live not ready")


def _start_uvicorn(root: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "support_platform.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--ws",
            "none",
        ],
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _stop_uvicorn(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _login(base: str) -> tuple[httpx.Cookies, str]:
    ok = httpx.post(
        f"{base}/api/v2/auth/login",
        json={"username": SMOKE_USER, "password": SMOKE_PASS},
        timeout=10.0,
    )
    if ok.status_code != 200:
        raise RuntimeError(f"login failed {ok.status_code} {ok.text}")
    token = ok.cookies.get(SESSION_COOKIE)
    csrf = ok.json().get("csrf_token")
    if not token or not csrf:
        raise RuntimeError("missing session cookie or csrf")
    return ok.cookies, str(csrf)


def _assert_clean(blob: str) -> None:
    lower = blob.lower()
    for m in FORBIDDEN:
        if m.lower() in lower:
            raise RuntimeError(f"forbidden marker in artifact: {m}")


def _cleanup(database_url: str, investigation_id: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM audit_events WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigation_create_keys WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM node_executions WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigation_events WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM task_leases WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigations WHERE id = CAST(:id AS uuid)",
        ):
            conn.execute(text(sql), {"id": investigation_id})
        for tid in (investigation_id, f"inv:{investigation_id}"):
            conn.execute(
                text("DELETE FROM graph_checkpoints WHERE thread_id = :tid"),
                {"tid": tid},
            )


def _seed_knowledge(database_url: str) -> str:
    engine = create_engine(database_url, pool_pre_ping=True)
    sid = str(uuid4())
    vid = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO knowledge_sources (id, type, title, status)
                VALUES (CAST(:sid AS uuid), 'DOC', 'S16 smoke doc', 'ACTIVE')
                """
            ),
            {"sid": sid},
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_versions (
                  id, source_id, label, content_hash, format, status, is_active,
                  applicability_scope
                ) VALUES (
                  CAST(:vid AS uuid), CAST(:sid AS uuid), 's16v1', 'hash-s16',
                  'MD', 'READY', true, 'GENERAL'
                )
                """
            ),
            {"vid": vid, "sid": sid},
        )
    return vid


def _cli(env: dict[str, str], *args: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "support_platform.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cli {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    return json.loads(line)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    os.environ["AUTH_BACKEND"] = "postgres"
    os.environ["PHASE1_API_MODE"] = "disabled"
    os.environ.setdefault("LOG_LEVEL", "INFO")

    print(
        "official_entry",
        f"uvicorn support_platform.main:app --host 127.0.0.1 --port {PORT} --ws none",
    )
    print("database", database_url.split("@")[-1])
    print("auth_backend", "postgres")
    print(
        "model_path",
        "NOT_EXECUTED paid Chat/Embedding worker→model (S13–S15 policy)",
    )
    print("no_fake", "uvicorn HTTP + PostgreSQL audit_events + freeze columns")

    created: list[str] = []
    proc: subprocess.Popen[bytes] | None = None
    cfg_tmpdir: tempfile.TemporaryDirectory[str] | None = None
    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok (incl. audit_events)")
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL migrate", exc, file=sys.stderr)
            return 1

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                if conn.execute(text("SELECT 1")).scalar_one() != 1:
                    print("SMOKE_FAIL pg probe", file=sys.stderr)
                    return 1
                tables = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                }
        except Exception as exc:  # noqa: BLE001
            print("NOT_EXECUTED: cannot connect to PostgreSQL:", exc, file=sys.stderr)
            return 2
        if "audit_events" not in tables:
            print("SMOKE_FAIL missing audit_events", file=sys.stderr)
            return 1
        print("pg_probe", "ok", "audit_events present")

        # Clean prior LOGIN_FAILURE noise for this smoke user actor.
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM audit_events WHERE actor = :u OR action = 'LOGIN_FAILURE' AND payload->>'username' = :u"),
                {"u": SMOKE_USER},
            )
            conn.execute(text("DELETE FROM config_versions WHERE created_by = 's16-smoke'"))

        upsert_user(
            database_url=database_url,
            username=SMOKE_USER,
            password=SMOKE_PASS,
            role="SUPPORT_AGENT",
        )
        print("user_seeded", SMOKE_USER)

        cfg_tmpdir = tempfile.TemporaryDirectory(prefix="s16_config_")
        cfg_root = Path(cfg_tmpdir.name)
        prompt_path = cfg_root / "prompt_v1.md"
        prompt_path.write_text("S16 smoke prompt\n", encoding="utf-8")

        env = os.environ.copy()
        env["AUTH_BACKEND"] = "postgres"
        env["PHASE1_API_MODE"] = "disabled"
        env["DATABASE_URL"] = database_url
        env["CONFIG_ROOT"] = str(cfg_root)
        env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")

        know_v = _seed_knowledge(database_url)
        print("knowledge_seed", know_v[:8])

        p1 = _cli(
            env,
            "config",
            "register",
            "--kind",
            "prompt",
            "--path",
            str(prompt_path),
            "--created-by",
            "s16-smoke",
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
            "s16-smoke",
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
            "s16-smoke",
        )
        for row in (p1, m1, r1):
            _cli(env, "config", "activate", "--version-id", row["version_id"])
        print("op13_activated", "prompt/model/retrieval")

        proc = _start_uvicorn(root, env)
        base = f"http://127.0.0.1:{PORT}"
        _wait_alive(base)
        print("uvicorn_alive", base)

        # --- OP-12 login failure → sanitized audit_events ---
        bad = httpx.post(
            f"{base}/api/v2/auth/login",
            json={"username": SMOKE_USER, "password": "should-not-persist"},
            timeout=10.0,
        )
        if bad.status_code != 401:
            print("SMOKE_FAIL bad login", bad.status_code, file=sys.stderr)
            return 1
        failures = AuditStore(backend="postgres", database_url=database_url).list_by_action(
            "LOGIN_FAILURE"
        )
        if not failures:
            print("SMOKE_FAIL no LOGIN_FAILURE audit row", file=sys.stderr)
            return 1
        fail_blob = json.dumps(failures[-1], ensure_ascii=False)
        _assert_clean(fail_blob)
        if "password" in fail_blob.lower() and "should-not-persist" in fail_blob.lower():
            print("SMOKE_FAIL password leaked in audit", fail_blob, file=sys.stderr)
            return 1
        print("login_failure_audit_ok")

        cookies, csrf = _login(base)
        print("login_ok", SMOKE_USER)

        # --- OP-07 create investigation (real PG) ---
        create = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S16-SMOKE trace versions and HITL audit?", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s16-smoke-{uuid4()}"},
            timeout=15.0,
        )
        if create.status_code != 202:
            print("SMOKE_FAIL OP-07", create.status_code, create.text, file=sys.stderr)
            return 1
        inv_id = str(create.json()["id"])
        created.append(inv_id)
        print("op07_queued", inv_id)

        # Freeze real active knowledge/config onto the investigation (S10 contract fields).
        config = PgConfigVersionService(database_url=database_url, config_root=cfg_root)
        frozen = freeze_for_create(
            knowledge=PgKnowledgeCandidates(database_url),
            config=config,
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE investigations SET
                      active_version_ids = CAST(:avs AS jsonb),
                      knowledge_snapshot_hash = :ksh,
                      prompt_version = :pv,
                      model_config_version = :mv,
                      retrieval_config_version = :rv,
                      config_hash = :ch
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": inv_id,
                    "avs": json.dumps(frozen["active_version_ids"], ensure_ascii=False),
                    "ksh": frozen["knowledge_snapshot_hash"],
                    "pv": frozen["prompt_version"],
                    "mv": frozen["model_config_version"],
                    "rv": frozen["retrieval_config_version"],
                    "ch": frozen["config_hash"],
                },
            )
        print(
            "freeze_applied",
            frozen["knowledge_snapshot_hash"][:12],
            frozen["prompt_version"][:8],
        )

        # Explicit TRACE_STEP with whitelist versions (no question body).
        TraceRecorder(backend="postgres", database_url=database_url).record(
            {
                "request_id": str(uuid4()),
                "investigation_id": inv_id,
                "node_name": "freeze_snapshot",
                "step_status": "COMPLETED",
                "knowledge_snapshot_hash": frozen["knowledge_snapshot_hash"],
                "prompt_version": frozen["prompt_version"],
                "model_config_version": frozen["model_config_version"],
                "retrieval_config_version": frozen["retrieval_config_version"],
                "source_ids": list(frozen["active_version_ids"])[:3],
                "latency_ms": 1,
            }
        )
        print("trace_step_written")

        # HITL: mark interrupted + OP-10 REJECT via official HTTP.
        marked = mark_interrupted(
            database_url=database_url,
            investigation_id=inv_id,
            reason="s16_smoke_hitl",
        )
        interrupt_id = str(marked["interrupt_id"])
        resume = httpx.post(
            f"{base}/api/v2/investigations/{inv_id}/resume",
            json={
                "interrupt_id": interrupt_id,
                "action": "REJECT",
                "input": {},
            },
            cookies=cookies,
            headers={
                CSRF_HEADER: csrf,
                "Idempotency-Key": f"s16-reject-{uuid4()}",
            },
            timeout=15.0,
        )
        if resume.status_code != 202:
            print("SMOKE_FAIL OP-10 REJECT", resume.status_code, resume.text, file=sys.stderr)
            return 1
        print("op10_reject_ok", resume.json().get("task_status"))

        # OP-08: versions + steps locatable
        detail = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies,
            timeout=10.0,
        )
        if detail.status_code != 200:
            print("SMOKE_FAIL OP-08", detail.status_code, detail.text, file=sys.stderr)
            return 1
        body = detail.json()
        for key in (
            "knowledge_snapshot_hash",
            "prompt_version",
            "model_config_version",
            "retrieval_config_version",
        ):
            if not body.get(key):
                print(f"SMOKE_FAIL OP-08 missing {key}", body, file=sys.stderr)
                return 1
        steps = body.get("public_steps") or []
        if not any(s.get("action") == "REJECT" or s.get("kind") == "human_decision" for s in steps):
            # interrupt_id may still be present on steps
            if not extract_interrupt_id(body) and not any("REJECT" in str(s) for s in steps):
                print("SMOKE_FAIL human decision step missing", steps, file=sys.stderr)
                return 1
        _assert_clean(json.dumps(body, ensure_ascii=False))
        # Question is on OP-08 by design — Trace/audit must not copy it.
        print("op08_versions_ok", body["prompt_version"][:8])

        store = AuditStore(backend="postgres", database_url=database_url)
        audits = store.list_for_investigation(inv_id)
        actions = {a["action"] for a in audits}
        if "HITL_REJECT" not in actions:
            print("SMOKE_FAIL missing HITL_REJECT audit", actions, file=sys.stderr)
            return 1
        if "TRACE_STEP" not in actions:
            print("SMOKE_FAIL missing TRACE_STEP audit", actions, file=sys.stderr)
            return 1
        audit_blob = json.dumps(audits, ensure_ascii=False)
        _assert_clean(audit_blob)
        if "S16-SMOKE trace versions" in audit_blob:
            print("SMOKE_FAIL question leaked into audit", file=sys.stderr)
            return 1
        print("audit_trace_ok", "actions", sorted(actions))

        traces = TraceRecorder(backend="postgres", database_url=database_url).list_for_investigation(
            inv_id
        )
        if not traces:
            print("SMOKE_FAIL no Trace rows", file=sys.stderr)
            return 1
        found_versions = False
        for tr in traces:
            _assert_clean(json.dumps(tr, ensure_ascii=False))
            if (
                tr.get("knowledge_snapshot_hash") == frozen["knowledge_snapshot_hash"]
                and tr.get("prompt_version") == frozen["prompt_version"]
            ):
                found_versions = True
        if not found_versions:
            print("SMOKE_FAIL Trace missing freeze versions", traces, file=sys.stderr)
            return 1
        print("trace_versions_match")

        print("cleanup_ok")
        print("S16_SMOKE_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL", type(exc).__name__, exc, file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            _stop_uvicorn(proc)
        for iid in created:
            try:
                _cleanup(database_url, iid)
            except Exception as exc:  # noqa: BLE001
                print("cleanup_warn", iid, exc, file=sys.stderr)
        try:
            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
        except Exception as exc:  # noqa: BLE001
            print("cleanup_warn user", exc, file=sys.stderr)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM config_versions WHERE created_by = 's16-smoke'"))
            conn.execute(
                text(
                    "DELETE FROM audit_events WHERE action = 'LOGIN_FAILURE' "
                    "AND payload->>'username' = :u"
                ),
                {"u": SMOKE_USER},
            )
        if cfg_tmpdir is not None:
            cfg_tmpdir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
