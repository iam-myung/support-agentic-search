"""S13 real-path SMOKE: v2 API → real PG Checkpointer → HITL supply/reject/cancel.

SPEC §8 S13 / approved PLAN: official uvicorn + real PostgreSQL checkpointer;
artificial Fake Redis/DB/model ports forbidden. Paid Chat→human_needed graph path
is NOT invoked here (PLAN: no paid Chat required; interrupt via production
mark_interrupted writing the real checkpoint). No high-risk write tools.
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
from support_platform.investigation.hitl import mark_interrupted
from support_platform.task_runtime import count_final_outputs
from support_platform.task_runtime.checkpointer import PostgresCheckpointerAdapter

SMOKE_USER = "s13_smoke_agent"
SMOKE_PASS = "s13-smoke-secret"
PORT = 8794


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
        for tid in (investigation_id, f"inv:{investigation_id}"):
            conn.execute(
                text("DELETE FROM graph_checkpoints WHERE thread_id = :tid"),
                {"tid": tid},
            )


def _create(base: str, cookies: httpx.Cookies, csrf: str, question: str) -> str:
    resp = httpx.post(
        f"{base}/api/v2/investigations",
        json={"question": question, "context": {}},
        cookies=cookies,
        headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s13-smoke-{uuid4()}"},
        timeout=15.0,
    )
    if resp.status_code != 202:
        raise RuntimeError(f"OP-07 failed {resp.status_code} {resp.text}")
    return str(resp.json()["id"])


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
    print("checkpointer", "PostgresCheckpointerAdapter → graph_checkpoints")
    print(
        "model_path",
        "NOT_EXECUTED paid Chat→human_needed (PLAN: no paid Chat; interrupt via mark_interrupted)",
    )
    print("no_fake", "OP-07/10/11 HTTP + real PG task_runtime + real PG checkpointer")
    print("no_write_tools", "HITL only SUPPLY/APPROVE/EDIT/REJECT/cancel")

    created: list[str] = []
    proc: subprocess.Popen[bytes] | None = None
    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok")
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL migrate", exc, file=sys.stderr)
            return 1

        # Prove PG reachable (not Fake).
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            n = conn.execute(text("SELECT 1")).scalar_one()
            if n != 1:
                print("SMOKE_FAIL pg probe", file=sys.stderr)
                return 1
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        if "graph_checkpoints" not in tables:
            print("SMOKE_FAIL graph_checkpoints missing", file=sys.stderr)
            return 1
        print("pg_probe", "ok")
        print("pg_tables_ok", "graph_checkpoints present")

        upsert_user(database_url=database_url, username=SMOKE_USER, password=SMOKE_PASS, role="SUPPORT_AGENT")
        print("user_seeded", SMOKE_USER)

        env = os.environ.copy()
        env["AUTH_BACKEND"] = "postgres"
        env["PHASE1_API_MODE"] = "disabled"
        env["DATABASE_URL"] = database_url
        proc = _start_uvicorn(root, env)
        base = f"http://127.0.0.1:{PORT}"
        _wait_alive(base)
        print("uvicorn_alive", base)

        cookies, csrf = _login(base)
        print("login_ok", SMOKE_USER)

        # --- Path A: illegal resume + valid SUPPLY ---
        inv_a = _create(base, cookies, csrf, "S13-SMOKE supply path?")
        created.append(inv_a)
        marked = mark_interrupted(
            database_url=database_url,
            investigation_id=inv_a,
            reason="human_needed",
            public_steps=[{"kind": "retrieve_1", "hit_count": 1}],
        )
        interrupt_id = marked["interrupt_id"]
        print("interrupted_a", inv_a, interrupt_id)

        adapter = PostgresCheckpointerAdapter(database_url=database_url)
        ckpt = adapter.get_checkpoint(thread_id=inv_a)
        if ckpt is None or ckpt.get("payload", {}).get("interrupt_id") != interrupt_id:
            print("SMOKE_FAIL checkpoint missing/mismatch", ckpt, file=sys.stderr)
            return 1
        print("checkpoint_ok", ckpt.get("checkpoint_id"))

        bad = httpx.post(
            f"{base}/api/v2/investigations/{inv_a}/resume",
            json={"interrupt_id": f"wrong-{uuid4()}", "action": "APPROVE", "input": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s13-bad-{uuid4()}"},
            timeout=15.0,
        )
        if bad.status_code != 409:
            print("SMOKE_FAIL wrong interrupt expected 409", bad.status_code, bad.text, file=sys.stderr)
            return 1
        print("op10_wrong_id", 409)

        supply = httpx.post(
            f"{base}/api/v2/investigations/{inv_a}/resume",
            json={
                "interrupt_id": interrupt_id,
                "action": "SUPPLY",
                "input": {"product_version": "2.1"},
            },
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s13-supply-{uuid4()}"},
            timeout=15.0,
        )
        if supply.status_code != 202:
            print("SMOKE_FAIL SUPPLY", supply.status_code, supply.text, file=sys.stderr)
            return 1
        if supply.json().get("task_status") not in {"QUEUED", "RUNNING"}:
            print("SMOKE_FAIL SUPPLY status", supply.json(), file=sys.stderr)
            return 1
        print("op10_supply", supply.json().get("task_status"))

        # --- Path B: REJECT must not be success ---
        inv_b = _create(base, cookies, csrf, "S13-SMOKE reject path?")
        created.append(inv_b)
        marked_b = mark_interrupted(
            database_url=database_url,
            investigation_id=inv_b,
            reason="human_needed",
            public_steps=[{"kind": "retrieve_1", "hit_count": 2}],
        )
        reject = httpx.post(
            f"{base}/api/v2/investigations/{inv_b}/resume",
            json={
                "interrupt_id": marked_b["interrupt_id"],
                "action": "REJECT",
                "input": {"reason": "customer declined"},
            },
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s13-reject-{uuid4()}"},
            timeout=15.0,
        )
        if reject.status_code != 202:
            print("SMOKE_FAIL REJECT", reject.status_code, reject.text, file=sys.stderr)
            return 1
        got_b = httpx.get(f"{base}/api/v2/investigations/{inv_b}", cookies=cookies, timeout=10.0)
        body_b = got_b.json()
        if body_b.get("task_status") == "COMPLETED" and body_b.get("result_status") == "ANSWERED":
            print("SMOKE_FAIL REJECT recorded as success", body_b, file=sys.stderr)
            return 1
        if body_b.get("task_status") not in {"FAILED", "CANCELLED", "COMPLETED"}:
            print("SMOKE_FAIL REJECT not terminal", body_b, file=sys.stderr)
            return 1
        finals = count_final_outputs(database_url=database_url, investigation_id=inv_b)
        if finals != 0:
            print("SMOKE_FAIL REJECT created final_output", finals, file=sys.stderr)
            return 1
        steps_b = body_b.get("public_steps") or []
        if not any(s.get("kind") == "retrieve_1" for s in steps_b):
            print("SMOKE_FAIL REJECT erased steps", steps_b, file=sys.stderr)
            return 1
        print("op10_reject", body_b.get("task_status"), "finals", finals)

        # --- Path C: cancel preserves evidence ---
        inv_c = _create(base, cookies, csrf, "S13-SMOKE cancel path?")
        created.append(inv_c)
        mark_interrupted(
            database_url=database_url,
            investigation_id=inv_c,
            reason="human_needed",
            public_steps=[{"kind": "retrieve_1", "hit_count": 3}],
        )
        cancel = httpx.post(
            f"{base}/api/v2/investigations/{inv_c}/cancel",
            json={},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s13-cancel-{uuid4()}"},
            timeout=15.0,
        )
        if cancel.status_code != 202 or cancel.json().get("task_status") != "CANCELLED":
            print("SMOKE_FAIL cancel", cancel.status_code, cancel.text, file=sys.stderr)
            return 1
        got_c = httpx.get(f"{base}/api/v2/investigations/{inv_c}", cookies=cookies, timeout=10.0)
        steps_c = got_c.json().get("public_steps") or []
        if not any(s.get("kind") == "retrieve_1" for s in steps_c):
            print("SMOKE_FAIL cancel erased steps", steps_c, file=sys.stderr)
            return 1
        print("op11_cancel", "CANCELLED", "steps_retained")

        print("S13_SMOKE_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL", type(exc).__name__, exc, file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            _stop_uvicorn(proc)
            print("uvicorn_stopped")
        for iid in created:
            try:
                _cleanup_investigation(database_url, iid)
            except Exception as exc:  # noqa: BLE001
                print("cleanup_warn", iid, exc, file=sys.stderr)
        if created:
            print("cleanup_ok", len(created))
        try:
            delete_user_by_username(database_url=database_url, username=SMOKE_USER)
            print("user_cleaned", SMOKE_USER)
        except Exception as exc:  # noqa: BLE001
            print("user_cleanup_warn", exc, file=sys.stderr)
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
