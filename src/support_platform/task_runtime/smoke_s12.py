"""S12 real-path SMOKE: v2 API → worker → real Redis/PG; Redis loss → PG scan → terminal state.

Paid Chat/Embedding is NOT invoked here (PLAN: model separately approved). Worker uses
deterministic_final. Fake Redis/DB/model ports are forbidden.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.pg_backend import delete_user_by_username, upsert_user
from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.task_runtime.lease import claim_next, scan_due_tasks
from support_platform.task_runtime.redis_adapter import RedisWakeAdapter
from support_platform.worker import run_once

SMOKE_USER = "s12_smoke_agent"
SMOKE_PASS = "s12-smoke-secret"
PORT = 8793
DEFAULT_REDIS = "redis://127.0.0.1:6379/0"


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
        for sql in (
            "DELETE FROM investigation_create_keys WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM node_executions WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigation_events WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM task_leases WHERE investigation_id = CAST(:id AS uuid)",
            "DELETE FROM investigations WHERE id = CAST(:id AS uuid)",
        ):
            conn.execute(text(sql), {"id": investigation_id})
        conn.execute(
            text("DELETE FROM graph_checkpoints WHERE thread_id = :tid"),
            {"tid": f"inv:{investigation_id}"},
        )


def _require_real_redis(redis_url: str) -> RedisWakeAdapter:
    adapter = RedisWakeAdapter(redis_url=redis_url)
    health = adapter.health()
    if not health.get("ok") or health.get("degraded"):
        print(
            "NOT_EXECUTED: real Redis unavailable",
            health,
            file=sys.stderr,
        )
        raise SystemExit(3)
    # Prove redis-py talks protocol (not socket-only fake).
    client = adapter._client()
    if client is None:
        print("NOT_EXECUTED: redis package missing", file=sys.stderr)
        raise SystemExit(3)
    client.ping()
    print("redis_health", health)
    print("redis_ping", "PONG")
    return adapter


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    redis_url = os.environ.get("REDIS_URL", "").strip() or DEFAULT_REDIS
    os.environ["REDIS_URL"] = redis_url

    prev_auth = os.environ.get("AUTH_BACKEND")
    prev_phase1 = os.environ.get("PHASE1_API_MODE")
    os.environ["AUTH_BACKEND"] = "postgres"
    os.environ["PHASE1_API_MODE"] = "disabled"
    os.environ.setdefault("LOG_LEVEL", "INFO")

    print(
        "official_entry_web",
        f"uvicorn support_platform.main:app --host 127.0.0.1 --port {PORT} --ws none",
    )
    print("official_entry_worker", "python -m support_platform.worker --once")
    print("database", database_url.split("@")[-1])
    print("redis_url", redis_url)
    print("auth_backend", "postgres")
    print("worker_mode", "deterministic_final (no paid model; PLAN deferred)")
    print("no_fake", "real Redis + real PostgreSQL + official worker entry")

    inv_id: str | None = None
    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok")
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL migrate", exc, file=sys.stderr)
            return 1

        try:
            adapter = _require_real_redis(redis_url)
        except SystemExit as exc:
            return int(exc.code)

        env = os.environ.copy()
        env["AUTH_BACKEND"] = "postgres"
        env["PHASE1_API_MODE"] = "disabled"
        env["DATABASE_URL"] = database_url
        env["REDIS_URL"] = redis_url
        base = f"http://127.0.0.1:{PORT}"

        delete_user_by_username(database_url=database_url, username=SMOKE_USER)
        user = upsert_user(
            database_url=database_url,
            username=SMOKE_USER,
            password=SMOKE_PASS,
            role="SUPPORT_AGENT",
        )
        print("seed_user", SMOKE_USER, str(user.id))

        proc = _start_uvicorn(root, env)
        try:
            _wait_alive(base)
            print("uvicorn_ready", base)

            token, csrf = _login(base)
            print("login_ok")

            create = httpx.post(
                f"{base}/api/v2/investigations",
                json={
                    "question": "S12 smoke: Redis flush then PG scan recovers?",
                    "context": {"symptom": "redis_loss"},
                },
                cookies={SESSION_COOKIE: token},
                headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s12-smoke-{uuid4()}"},
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

            woke = adapter.enqueue_wake(investigation_id=inv_id)
            print("redis_wake_enqueued", woke)
            if woke is not True:
                print("SMOKE_FAIL redis wake enqueue expected True", woke, file=sys.stderr)
                return 1

            # Simulate Redis wake-key loss — task truth must remain in PostgreSQL.
            adapter.flush_wakeup_queue()
            print("redis_wakeup_flushed")

            due = scan_due_tasks(database_url=database_url)
            due_ids = {str(item.get("investigation_id") or item.get("id")) for item in due}
            if inv_id not in due_ids:
                print("SMOKE_FAIL PG scan missing queued task", due, file=sys.stderr)
                return 1
            print("pg_scan_recovered", inv_id)

            # Kill web process mid-queue; worker + PG remain authoritative.
            _stop_uvicorn(proc)
            print("web_killed")

            lease_seconds = int(os.environ.get("WORKER_LEASE_SECONDS", "60") or "60")
            result = run_once(
                database_url=database_url,
                worker_id="s12-smoke-worker",
                lease_seconds=lease_seconds,
            )
            print("worker_once", result)
            if result is None or result.get("task_status") != "COMPLETED":
                print("SMOKE_FAIL worker did not complete", result, file=sys.stderr)
                return 1

            # Restart web — OP-08 must show clear terminal state.
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
                        "SMOKE_FAIL OP-08 after restart",
                        after.status_code,
                        after.text,
                        file=sys.stderr,
                    )
                    return 1
                status = after.json().get("task_status")
                if status != "COMPLETED":
                    print("SMOKE_FAIL expected COMPLETED after restart", status, file=sys.stderr)
                    return 1
                print("op08_final", status)

                # Second claim must not duplicate finals for same investigation.
                second_claim = claim_next(
                    database_url=database_url,
                    worker_id="s12-smoke-worker-b",
                    lease_seconds=lease_seconds,
                )
                print("post_complete_claim", second_claim)
                if second_claim is not None and str(second_claim.get("investigation_id")) == inv_id:
                    print(
                        "SMOKE_FAIL completed task reclaimed",
                        second_claim,
                        file=sys.stderr,
                    )
                    return 1

                print("S12_SMOKE_OK")
                return 0
            finally:
                _stop_uvicorn(proc2)
                print("cleanup_uvicorn", "proc2 terminated")
        finally:
            if proc.poll() is None:
                _stop_uvicorn(proc)
                print("cleanup_uvicorn", "proc1 terminated")
            adapter.flush_wakeup_queue()
            print("cleanup_redis_wakeup")
    finally:
        if inv_id:
            _cleanup_investigation(database_url, inv_id)
            print("cleanup_investigation", inv_id)
        delete_user_by_username(database_url=database_url, username=SMOKE_USER)
        print("cleanup_pg_user", SMOKE_USER)
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
