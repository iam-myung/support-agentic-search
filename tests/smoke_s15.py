"""S15 real-path SMOKE: official uvicorn workbench → v2 login/SSE/HITL → real PG/Redis.

SPEC §8 S15 / AC-010 / AC-011 (UI):
- GET / serves Phase-2 login + progress + HITL markers and workbench.js
- OP-12 login against real PostgreSQL (no auth bypass)
- OP-07 create → OP-09 SSE catch-up (Last-Event-ID) on real PG events + Redis wake
- OP-10 HITL SUPPLY / REJECT via same endpoints the UI wires
- No unverified-draft hooks in UI assets; SSE payloads sanitized

Paid Chat/Embedding / full worker→model path: NOT_EXECUTED (same policy as S13/S14 PLAN).
Fake Redis/DB/TestClient-as-only-entry forbidden for release evidence.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text

from support_platform.auth import CSRF_HEADER, SESSION_COOKIE
from support_platform.auth.pg_backend import delete_user_by_username, upsert_user
from support_platform.infrastructure.db.migrate import upgrade_head
from support_platform.investigation.hitl import mark_interrupted
from support_platform.task_runtime import persist_final_output
from support_platform.task_runtime.events import append_answer_chunk, append_event
from support_platform.task_runtime.redis_adapter import RedisWakeAdapter

SMOKE_USER = "s15_smoke_agent"
SMOKE_PASS = "s15-smoke-secret"
PORT = 8796
DEFAULT_REDIS = "redis://127.0.0.1:6379/0"

FORBIDDEN_SSE = (
    "chain of thought",
    "internal reasoning",
    "api_key",
    "sk-",
    "LLM_API_KEY",
    "unauthorized_source",
)

FORBIDDEN_UI = (
    "unverified draft",
    "stream_draft",
    "raw_model_stream",
    "fake_typewriter_before_validation",
    "pre_validation_chunk",
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


def _parse_sse(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in re.split(r"\n\n+", body.strip()):
        if not block.strip():
            continue
        seq: int | None = None
        etype: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("id:"):
                seq = int(line[3:].strip())
            elif line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        payload: dict[str, Any] = {}
        if data_lines:
            raw = "\n".join(data_lines)
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"_raw": raw}
        frames.append({"id": seq, "event": etype, "data": payload})
    return frames


def _sse_get(
    base: str,
    *,
    inv_id: str,
    cookies: httpx.Cookies,
    last_event_id: int | None = None,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    with httpx.Client(timeout=timeout, cookies=cookies) as client:
        with client.stream(
            "GET",
            f"{base}/api/v2/investigations/{inv_id}/events",
            headers=headers,
            params={"snapshot": "1"},
        ) as resp:
            ctype = resp.headers.get("content-type", "")
            text_body = "".join(resp.iter_text())
            return resp.status_code, ctype, text_body


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


def _require_real_redis(redis_url: str) -> RedisWakeAdapter:
    adapter = RedisWakeAdapter(redis_url=redis_url)
    health = adapter.health()
    if not health.get("ok") or health.get("degraded"):
        print("NOT_EXECUTED: real Redis unavailable", health, file=sys.stderr)
        raise SystemExit(3)
    client = adapter._client()
    if client is None:
        print("NOT_EXECUTED: redis package missing", file=sys.stderr)
        raise SystemExit(3)
    client.ping()
    print("redis_probe", "ok", redis_url.split("@")[-1])
    return adapter


def _assert_no_forbidden(blob: str, markers: tuple[str, ...]) -> None:
    lower = blob.lower()
    for m in markers:
        if m.lower() in lower:
            raise RuntimeError(f"forbidden marker in artifact: {m}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root / ".env")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("NOT_EXECUTED: DATABASE_URL missing", file=sys.stderr)
        return 2

    redis_url = os.environ.get("REDIS_URL", "").strip() or DEFAULT_REDIS
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
    print("redis", redis_url.split("@")[-1])
    print("auth_backend", "postgres")
    print(
        "model_path",
        "NOT_EXECUTED paid Chat/Embedding worker→model (S13/S14 policy; SSE+HITL via real PG)",
    )
    print("no_fake", "uvicorn HTTP + PostgreSQL + Redis; UI assets from / and /static")

    created: list[str] = []
    proc: subprocess.Popen[bytes] | None = None
    try:
        try:
            upgrade_head(database_url)
            print("migrate", "upgrade head ok")
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_FAIL migrate", exc, file=sys.stderr)
            return 1

        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1")).scalar_one() != 1:
                print("SMOKE_FAIL pg probe", file=sys.stderr)
                return 1
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        for need in ("users", "sessions", "investigation_events", "graph_checkpoints"):
            if need not in tables:
                print(f"SMOKE_FAIL missing table {need}", file=sys.stderr)
                return 1
        print("pg_probe", "ok")

        try:
            adapter = _require_real_redis(redis_url)
        except SystemExit as exc:
            return int(exc.code)

        upsert_user(
            database_url=database_url,
            username=SMOKE_USER,
            password=SMOKE_PASS,
            role="SUPPORT_AGENT",
        )
        print("user_seeded", SMOKE_USER)

        env = os.environ.copy()
        env["AUTH_BACKEND"] = "postgres"
        env["PHASE1_API_MODE"] = "disabled"
        env["DATABASE_URL"] = database_url
        env["REDIS_URL"] = redis_url

        proc = _start_uvicorn(root, env)
        base = f"http://127.0.0.1:{PORT}"
        _wait_alive(base)
        print("uvicorn_alive", base)

        # --- Browser entry: workbench HTML + static assets ---
        home = httpx.get(f"{base}/", timeout=10.0)
        if home.status_code != 200 or "text/html" not in (home.headers.get("content-type") or ""):
            print("SMOKE_FAIL workbench GET", home.status_code, file=sys.stderr)
            return 1
        html = home.text
        for marker in (
            'id="login-form"',
            'name="username"',
            'type="password"',
            "data-sse",
            "data-hitl",
            "SUPPLY",
            "APPROVE",
            "EDIT",
            "REJECT",
            "workbench.js",
        ):
            if marker not in html:
                print("SMOKE_FAIL workbench missing", marker, file=sys.stderr)
                return 1
        print("workbench_html_ok")

        css = httpx.get(f"{base}/static/workbench.css", timeout=10.0)
        js = httpx.get(f"{base}/static/workbench.js", timeout=10.0)
        if css.status_code != 200 or js.status_code != 200:
            print("SMOKE_FAIL static", css.status_code, js.status_code, file=sys.stderr)
            return 1
        js_text = js.text
        for need in ("EventSource", "Last-Event-ID", "ANSWER_CHUNK", "/api/v2/auth/login", "/resume"):
            if need not in js_text:
                print("SMOKE_FAIL workbench.js missing", need, file=sys.stderr)
                return 1
        _assert_no_forbidden(html + "\n" + js_text, FORBIDDEN_UI)
        print("static_js_css_ok", "no_unverified_draft_hooks")

        # --- OP-12 login (real PG sessions) ---
        bad = httpx.post(
            f"{base}/api/v2/auth/login",
            json={"username": SMOKE_USER, "password": "wrong"},
            timeout=10.0,
        )
        if bad.status_code != 401:
            print("SMOKE_FAIL bad login expected 401", bad.status_code, file=sys.stderr)
            return 1
        print("login_bad_credentials", 401)

        cookies, csrf = _login(base)
        print("login_ok", SMOKE_USER, "HttpOnly session")

        # --- OP-07 + SSE progress (real PG events, Redis wake) ---
        create = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S15-SMOKE UI progress + HITL?", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s15-smoke-{uuid4()}"},
            timeout=15.0,
        )
        if create.status_code != 202:
            print("SMOKE_FAIL OP-07", create.status_code, create.text, file=sys.stderr)
            return 1
        inv_id = str(create.json()["id"])
        created.append(inv_id)
        print("op07_queued", inv_id)

        adapter.flush_wakeup_queue()
        started = append_event(
            database_url=database_url,
            investigation_id=inv_id,
            event_type="STARTED",
            public_payload={
                "task_status": "RUNNING",
                "api_key": "sk-should-never-leak",
                "internal_reasoning": "chain of thought hidden",
            },
        )
        step1 = append_event(
            database_url=database_url,
            investigation_id=inv_id,
            event_type="STEP",
            public_payload={"kind": "retrieve_1", "hit_count": 2},
        )
        print("events_appended", started["sequence"], step1["sequence"])

        code1, ctype1, body1 = _sse_get(base, inv_id=inv_id, cookies=cookies, last_event_id=0)
        if code1 != 200 or "text/event-stream" not in ctype1:
            print("SMOKE_FAIL SSE", code1, ctype1, body1[:300], file=sys.stderr)
            return 1
        frames1 = _parse_sse(body1)
        _assert_no_forbidden(body1, FORBIDDEN_SSE)
        types1 = [f["event"] for f in frames1]
        if "STARTED" not in types1 or "STEP" not in types1:
            print("SMOKE_FAIL SSE types", types1, file=sys.stderr)
            return 1
        last_seen = int(step1["sequence"])
        print("sse_progress_ok", "frames", len(frames1), "sanitized")

        chunk = append_answer_chunk(
            database_url=database_url,
            investigation_id=inv_id,
            chunk_text="Validated pilot reply chunk.",
            validated=True,
            result_status="ANSWERED",
            evidence_ids=["ev-s15-1"],
        )
        append_event(
            database_url=database_url,
            investigation_id=inv_id,
            event_type="COMPLETED",
            public_payload={"task_status": "COMPLETED", "result_status": "ANSWERED"},
        )
        persist_final_output(
            database_url=database_url,
            investigation_id=inv_id,
            idempotency_key=f"s15-smoke-final-{uuid4()}",
            result_status="ANSWERED",
            output={"summary": "S15 smoke validated reply"},
            public_steps=[{"kind": "retrieve_1", "hit_count": 2}],
        )
        print("answer_chunk_validated", chunk["sequence"])

        code2, _ctype2, body2 = _sse_get(
            base, inv_id=inv_id, cookies=cookies, last_event_id=last_seen
        )
        if code2 != 200:
            print("SMOKE_FAIL reconnect SSE", code2, body2[:200], file=sys.stderr)
            return 1
        frames2 = _parse_sse(body2)
        _assert_no_forbidden(body2, FORBIDDEN_SSE)
        types2 = [f["event"] for f in frames2]
        if "ANSWER_CHUNK" not in types2 or "COMPLETED" not in types2:
            print("SMOKE_FAIL catch-up types", types2, file=sys.stderr)
            return 1
        print("sse_last_event_id_catchup_ok", types2)

        op08 = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies,
            timeout=10.0,
        )
        if op08.status_code != 200 or op08.json().get("task_status") != "COMPLETED":
            print("SMOKE_FAIL OP-08", op08.status_code, op08.text[:200], file=sys.stderr)
            return 1
        print("op08_terminal_ok", op08.json().get("result_status"))

        # --- HITL path (UI-wired OP-10): SUPPLY + REJECT ---
        inv_hitl = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S15-SMOKE HITL interrupt?", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s15-hitl-{uuid4()}"},
            timeout=15.0,
        )
        if inv_hitl.status_code != 202:
            print("SMOKE_FAIL HITL create", inv_hitl.status_code, inv_hitl.text, file=sys.stderr)
            return 1
        hitl_id = str(inv_hitl.json()["id"])
        created.append(hitl_id)
        marked = mark_interrupted(
            database_url=database_url,
            investigation_id=hitl_id,
            reason="human_needed",
            public_steps=[{"kind": "retrieve_1", "hit_count": 1}],
        )
        interrupt_id = marked["interrupt_id"]
        append_event(
            database_url=database_url,
            investigation_id=hitl_id,
            event_type="REVIEW_REQUIRED",
            public_payload={"interrupt_id": interrupt_id, "summary": "needs human"},
        )
        print("hitl_interrupted", hitl_id, interrupt_id)

        supply = httpx.post(
            f"{base}/api/v2/investigations/{hitl_id}/resume",
            json={
                "interrupt_id": interrupt_id,
                "action": "SUPPLY",
                "input": {"product_version": "2.1"},
            },
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s15-supply-{uuid4()}"},
            timeout=15.0,
        )
        if supply.status_code != 202:
            print("SMOKE_FAIL SUPPLY", supply.status_code, supply.text, file=sys.stderr)
            return 1
        print("op10_supply", supply.json().get("task_status"))

        inv_rej = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S15-SMOKE REJECT path?", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s15-rej-{uuid4()}"},
            timeout=15.0,
        )
        if inv_rej.status_code != 202:
            print("SMOKE_FAIL REJECT create", inv_rej.status_code, file=sys.stderr)
            return 1
        rej_id = str(inv_rej.json()["id"])
        created.append(rej_id)
        marked_r = mark_interrupted(
            database_url=database_url,
            investigation_id=rej_id,
            reason="human_needed",
            public_steps=[{"kind": "retrieve_1"}],
        )
        reject = httpx.post(
            f"{base}/api/v2/investigations/{rej_id}/resume",
            json={
                "interrupt_id": marked_r["interrupt_id"],
                "action": "REJECT",
                "input": {},
            },
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s15-reject-{uuid4()}"},
            timeout=15.0,
        )
        if reject.status_code != 202:
            print("SMOKE_FAIL REJECT", reject.status_code, reject.text, file=sys.stderr)
            return 1
        rej_body = reject.json()
        if rej_body.get("task_status") == "COMPLETED" and rej_body.get("result_status") == "ANSWERED":
            print("SMOKE_FAIL REJECT recorded as success", rej_body, file=sys.stderr)
            return 1
        print("op10_reject_not_success", rej_body.get("task_status"), rej_body.get("result_status"))

        print("S15_SMOKE_OK")
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
                print("cleanup_investigation", iid)
            except Exception as exc:  # noqa: BLE001
                print("cleanup_warn", iid, exc, file=sys.stderr)
        try:
            RedisWakeAdapter(redis_url=redis_url).flush_wakeup_queue()
            print("cleanup_redis_wakeup")
        except Exception as exc:  # noqa: BLE001
            print("cleanup_redis_warn", exc, file=sys.stderr)
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
