"""S11 real-path SMOKE: official uvicorn → OP-07/08 → real PostgreSQL; kill process, state remains."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.pg_backend import delete_user_by_username, upsert_user
from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.task_runtime import (
    count_final_outputs,
    mark_running,
    persist_final_output,
)
from support_platform.task_runtime.checkpointer import PostgresCheckpointerAdapter

REQUIRED_TABLES = {
    "task_leases",
    "investigation_events",
    "node_executions",
    "graph_checkpoints",
    "investigation_create_keys",
}
SMOKE_USER = "s11_smoke_agent"
SMOKE_PASS = "s11-smoke-secret"
PORT = 8792


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


def _login(base: str) -> tuple[str, str]:
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
    return token, str(csrf)


def _cleanup_investigation(database_url: str, investigation_id: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM investigation_create_keys WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM node_executions WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM investigation_events WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM task_leases WHERE investigation_id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )
        conn.execute(
            text("DELETE FROM graph_checkpoints WHERE thread_id = :tid"),
            {"tid": f"inv:{investigation_id}"},
        )
        conn.execute(
            text("DELETE FROM investigations WHERE id = CAST(:id AS uuid)"),
            {"id": investigation_id},
        )


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    prev_auth = os.environ.get("AUTH_BACKEND")
    prev_phase1 = os.environ.get("PHASE1_API_MODE")
    os.environ["AUTH_BACKEND"] = "postgres"
    os.environ["PHASE1_API_MODE"] = "disabled"
    os.environ.setdefault("LOG_LEVEL", "INFO")

    print(
        "official_entry",
        f"uvicorn support_platform.main:app --host 127.0.0.1 --port {PORT} --ws none",
    )
    print("database", database_url.split("@")[-1])
    print("auth_backend", "postgres")
    print("dependency", "real PostgreSQL (no Redis required for S11 task truth)")
    print("no_fake", "OP-07/08 HTTP + PG task_runtime + PG checkpointer")

    inv_id: str | None = None
    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok")
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
                missing = REQUIRED_TABLES - tables
                if missing:
                    print(f"SMOKE_FAIL missing tables: {sorted(missing)}", file=sys.stderr)
                    return 1
            print("tables_ok", sorted(REQUIRED_TABLES))

            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
            user = upsert_user(
                database_url=database_url,
                username=SMOKE_USER,
                password=SMOKE_PASS,
                role="SUPPORT_AGENT",
            )
            print("seed_user", SMOKE_USER, str(user.id))

            env = os.environ.copy()
            env["AUTH_BACKEND"] = "postgres"
            env["PHASE1_API_MODE"] = "disabled"
            env["DATABASE_URL"] = database_url
            base = f"http://127.0.0.1:{PORT}"

            proc = _start_uvicorn(root, env)
            try:
                _wait_alive(base)
                print("uvicorn_ready", base)

                token, csrf = _login(base)
                print("login_ok")

                create = httpx.post(
                    f"{base}/api/v2/investigations",
                    json={
                        "question": "S11 smoke: can task survive process kill?",
                        "context": {"symptom": "restart"},
                    },
                    cookies={SESSION_COOKIE: token},
                    headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s11-smoke-{uuid4()}"},
                    timeout=10.0,
                )
                if create.status_code != 202:
                    print("SMOKE_FAIL OP-07", create.status_code, create.text, file=sys.stderr)
                    return 1
                body = create.json()
                if body.get("task_status") != "QUEUED" or not body.get("id"):
                    print("SMOKE_FAIL OP-07 body", body, file=sys.stderr)
                    return 1
                inv_id = str(body["id"])
                print("op07_queued", inv_id)

                got = httpx.get(
                    f"{base}/api/v2/investigations/{inv_id}",
                    cookies={SESSION_COOKIE: token},
                    timeout=10.0,
                )
                if got.status_code != 200 or got.json().get("task_status") != "QUEUED":
                    print("SMOKE_FAIL OP-08 before kill", got.status_code, got.text, file=sys.stderr)
                    return 1
                print("op08_before_kill", "QUEUED")

                mark_running(database_url=database_url, investigation_id=inv_id)
                adapter = PostgresCheckpointerAdapter(database_url=database_url)
                thread_id = adapter.thread_id_for(UUID(inv_id))
                adapter.put_checkpoint(
                    thread_id=thread_id,
                    checkpoint_id="ckpt-s11-smoke-1",
                    payload={"node": "retrieve_1", "public_steps": [{"step": "retrieve"}]},
                )
                print("checkpoint_written", thread_id)

                # Kill process — task truth must remain in PostgreSQL only.
                _stop_uvicorn(proc)
                print("process_killed", "uvicorn terminated")
                del adapter

                # Fresh process + fresh adapter (no in-memory reuse).
                proc2 = _start_uvicorn(root, env)
                try:
                    _wait_alive(base)
                    print("uvicorn_restarted", base)

                    token2, _csrf2 = _login(base)
                    after = httpx.get(
                        f"{base}/api/v2/investigations/{inv_id}",
                        cookies={SESSION_COOKIE: token2},
                        timeout=10.0,
                    )
                    if after.status_code != 200:
                        print(
                            "SMOKE_FAIL OP-08 after kill",
                            after.status_code,
                            after.text,
                            file=sys.stderr,
                        )
                        return 1
                    after_body = after.json()
                    status = after_body.get("task_status")
                    if status not in {"RUNNING", "INTERRUPTED", "QUEUED", "FAILED"}:
                        print("SMOKE_FAIL unidentifiable status after kill", status, file=sys.stderr)
                        return 1
                    print("op08_after_kill", status)

                    adapter_b = PostgresCheckpointerAdapter(database_url=database_url)
                    restored = adapter_b.get_checkpoint(thread_id=thread_id)
                    if restored is None or restored.get("checkpoint_id") != "ckpt-s11-smoke-1":
                        print("SMOKE_FAIL checkpoint lost after kill", restored, file=sys.stderr)
                        return 1
                    print("checkpoint_restored", restored["checkpoint_id"])

                    first = persist_final_output(
                        database_url=database_url,
                        investigation_id=inv_id,
                        idempotency_key=f"final:{inv_id}",
                        result_status="ANSWERED",
                        output={"summary": "smoke-once"},
                        public_steps=[{"step": "compose"}],
                    )
                    second = persist_final_output(
                        database_url=database_url,
                        investigation_id=inv_id,
                        idempotency_key=f"final:{inv_id}",
                        result_status="ANSWERED",
                        output={"summary": "smoke-twice"},
                        public_steps=[{"step": "dup"}],
                    )
                    n = count_final_outputs(database_url=database_url, investigation_id=inv_id)
                    if not (
                        (first.get("accepted") is True or first.get("task_status") == "COMPLETED")
                        and (
                            second.get("accepted") is False
                            or second.get("duplicate") is True
                            or (second.get("output") or {}).get("summary") == "smoke-once"
                        )
                        and n == 1
                    ):
                        print(
                            "SMOKE_FAIL duplicate final",
                            first,
                            second,
                            n,
                            file=sys.stderr,
                        )
                        return 1
                    print("duplicate_final_blocked", "count", n)

                    final = httpx.get(
                        f"{base}/api/v2/investigations/{inv_id}",
                        cookies={SESSION_COOKIE: token2},
                        timeout=10.0,
                    )
                    if final.status_code != 200 or final.json().get("task_status") != "COMPLETED":
                        print("SMOKE_FAIL final OP-08", final.status_code, final.text, file=sys.stderr)
                        return 1
                    print("op08_final", "COMPLETED")

                    print("S11_SMOKE_OK")
                    return 0
                finally:
                    _stop_uvicorn(proc2)
                    print("cleanup_uvicorn", "proc2 terminated")
            finally:
                if proc.poll() is None:
                    _stop_uvicorn(proc)
                    print("cleanup_uvicorn", "proc1 terminated")
        finally:
            if inv_id:
                _cleanup_investigation(database_url, inv_id)
                print("cleanup_investigation", inv_id)
            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
            print("cleanup_pg_user", SMOKE_USER)
    finally:
        if prev_auth is None:
            os.environ.pop("AUTH_BACKEND", None)
        else:
            os.environ["AUTH_BACKEND"] = prev_auth
        if prev_phase1 is None:
            os.environ.pop("PHASE1_API_MODE", None)
        else:
            os.environ["PHASE1_API_MODE"] = prev_phase1


if __name__ == "__main__":
    raise SystemExit(main())
