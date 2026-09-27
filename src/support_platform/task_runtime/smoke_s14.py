"""S14 real-path SMOKE: OP-09 SSE catch-up via official uvicorn + real Redis/PG.

SPEC §8 S14 / AC-010: client disconnect → Last-Event-ID reconnect recovers newer
events from PostgreSQL; Redis wake is best-effort (not truth). Fake Redis/DB/model
ports forbidden. Paid Chat/Embedding NOT invoked (SSE event path only).
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
from support_platform.task_runtime import persist_final_output
from support_platform.task_runtime.events import append_answer_chunk, append_event
from support_platform.task_runtime.redis_adapter import RedisWakeAdapter

SMOKE_USER = "s14_smoke_agent"
SMOKE_PASS = "s14-smoke-secret"
PORT = 8795
DEFAULT_REDIS = "redis://127.0.0.1:6379/0"

FORBIDDEN_MARKERS = (
    "chain of thought",
    "internal reasoning",
    "api_key",
    "sk-",
    "LLM_API_KEY",
    "unauthorized_source",
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
    print("redis_health", health)
    print("redis_ping", "PONG")
    return adapter


def _assert_no_forbidden(text_blob: str) -> None:
    low = text_blob.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker.lower() in low:
            raise RuntimeError(f"sensitive marker leaked in SSE: {marker}")


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
        "official_entry",
        f"uvicorn support_platform.main:app --host 127.0.0.1 --port {PORT} --ws none",
    )
    print("database", database_url.split("@")[-1])
    print("redis_url", redis_url)
    print("auth_backend", "postgres")
    print("model_path", "NOT_EXECUTED paid Chat/Embedding (S14 SSE-only; PLAN deferred)")
    print("no_fake", "official uvicorn + real PG investigation_events + real Redis wake")

    inv_id: str | None = None
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
        if "investigation_events" not in tables:
            print("SMOKE_FAIL investigation_events missing", file=sys.stderr)
            return 1
        print("pg_probe", "ok")
        print("pg_tables_ok", "investigation_events present")

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

        cookies, csrf = _login(base)
        print("login_ok", SMOKE_USER)

        create = httpx.post(
            f"{base}/api/v2/investigations",
            json={"question": "S14-SMOKE disconnect catch-up?", "context": {}},
            cookies=cookies,
            headers={CSRF_HEADER: csrf, "Idempotency-Key": f"s14-smoke-{uuid4()}"},
            timeout=15.0,
        )
        if create.status_code != 202:
            print("SMOKE_FAIL OP-07", create.status_code, create.text, file=sys.stderr)
            return 1
        inv_id = str(create.json()["id"])
        print("op07_queued", inv_id)

        adapter.flush_wakeup_queue()
        print("redis_wakeup_flushed")

        # Phase A: append early events (PG truth + Redis wake).
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
        print("events_appended_a", started["sequence"], step1["sequence"])

        # Prove Redis received a wake from production append_event path.
        client = adapter._client()
        assert client is not None
        wakes = [x.decode() if isinstance(x, bytes) else str(x) for x in client.lrange("support_platform:task_wakeup", 0, -1)]
        if inv_id not in wakes:
            print("SMOKE_FAIL redis wake missing after append", wakes, file=sys.stderr)
            return 1
        print("redis_wake_after_append", True, "count", len(wakes))

        # Client connects, reads catch-up through STEP, then "disconnects".
        code1, ctype1, body1 = _sse_get(base, inv_id=inv_id, cookies=cookies, last_event_id=0)
        if code1 != 200 or "text/event-stream" not in ctype1:
            print("SMOKE_FAIL first SSE", code1, ctype1, body1[:300], file=sys.stderr)
            return 1
        frames1 = _parse_sse(body1)
        _assert_no_forbidden(body1)
        ids1 = [f["id"] for f in frames1 if f["id"] is not None]
        types1 = [f["event"] for f in frames1]
        if not ids1 or max(ids1) < int(step1["sequence"]):
            print("SMOKE_FAIL first catch-up incomplete", frames1, file=sys.stderr)
            return 1
        if "STARTED" not in types1 or "STEP" not in types1:
            print("SMOKE_FAIL missing STARTED/STEP", types1, file=sys.stderr)
            return 1
        last_seen = int(step1["sequence"])
        print("sse_phase_a", "frames", len(frames1), "last_seen", last_seen, "sanitized_ok")

        # Disconnect window: client gone; more events land in PG.
        chunk = append_answer_chunk(
            database_url=database_url,
            investigation_id=inv_id,
            chunk_text="Please restart the agent service.",
            validated=True,
            result_status="ANSWERED",
            evidence_ids=["ev-smoke-1"],
        )
        final_ev = append_event(
            database_url=database_url,
            investigation_id=inv_id,
            event_type="COMPLETED",
            public_payload={"task_status": "COMPLETED", "result_status": "ANSWERED"},
        )
        print("events_appended_b", chunk["sequence"], final_ev["sequence"])

        persist_final_output(
            database_url=database_url,
            investigation_id=inv_id,
            idempotency_key=f"s14-smoke-final-{uuid4()}",
            result_status="ANSWERED",
            output={
                "summary": "S14 smoke validated reply",
                "suggested_reply": "Please restart the agent service.",
            },
            public_steps=[{"kind": "retrieve_1", "hit_count": 2}],
        )
        print("op08_fact_written", "COMPLETED/ANSWERED")

        # Reconnect with Last-Event-ID = last_seen (browser catch-up).
        code2, ctype2, body2 = _sse_get(
            base, inv_id=inv_id, cookies=cookies, last_event_id=last_seen
        )
        if code2 != 200 or "text/event-stream" not in ctype2:
            print("SMOKE_FAIL reconnect SSE", code2, ctype2, body2[:300], file=sys.stderr)
            return 1
        frames2 = _parse_sse(body2)
        _assert_no_forbidden(body2)
        ids2 = [int(f["id"]) for f in frames2 if f["id"] is not None]
        types2 = [f["event"] for f in frames2]
        if any(i <= last_seen for i in ids2):
            print("SMOKE_FAIL catch-up replayed old ids", ids2, "last_seen", last_seen, file=sys.stderr)
            return 1
        if "ANSWER_CHUNK" not in types2 or "COMPLETED" not in types2:
            print("SMOKE_FAIL reconnect missing newer events", types2, file=sys.stderr)
            return 1
        print("sse_reconnect_catchup", "ids", ids2, "types", types2)

        # Terminal SSE vs OP-08 fact.
        op08 = httpx.get(
            f"{base}/api/v2/investigations/{inv_id}",
            cookies=cookies,
            timeout=10.0,
        )
        if op08.status_code != 200:
            print("SMOKE_FAIL OP-08", op08.status_code, op08.text, file=sys.stderr)
            return 1
        fact = op08.json()
        if fact.get("task_status") != "COMPLETED":
            print("SMOKE_FAIL OP-08 not COMPLETED", fact, file=sys.stderr)
            return 1
        completed_frames = [f for f in frames2 if f["event"] == "COMPLETED"]
        if not completed_frames:
            print("SMOKE_FAIL no COMPLETED frame on reconnect", file=sys.stderr)
            return 1
        term_payload = completed_frames[-1]["data"]
        if term_payload.get("task_status") != fact.get("task_status"):
            print(
                "SMOKE_FAIL terminal SSE != OP-08",
                term_payload,
                fact.get("task_status"),
                file=sys.stderr,
            )
            return 1
        print("terminal_consistent", fact.get("task_status"), fact.get("result_status"))

        # Stale Last-Event-ID must prompt OP-08 re-read (not silent empty).
        stale_id = max(ids2) + 100 if ids2 else 9999
        code3, _ctype3, body3 = _sse_get(
            base, inv_id=inv_id, cookies=cookies, last_event_id=stale_id
        )
        if code3 != 200:
            print("SMOKE_FAIL stale SSE status", code3, body3[:200], file=sys.stderr)
            return 1
        frames3 = _parse_sse(body3)
        if not frames3 or frames3[0].get("event") != "gone":
            print("SMOKE_FAIL stale expected gone", frames3, file=sys.stderr)
            return 1
        detail = str(frames3[0].get("data", {}).get("detail", "")).lower()
        op08_hint = str(frames3[0].get("data", {}).get("op08", ""))
        if "op-08" not in detail and "op08" not in detail.replace("-", ""):
            # accept either wording
            if "re-read" not in detail and "reread" not in detail.replace("-", ""):
                print("SMOKE_FAIL stale detail missing re-read hint", frames3[0], file=sys.stderr)
                return 1
        if inv_id not in op08_hint:
            print("SMOKE_FAIL stale missing OP-08 path", frames3[0], file=sys.stderr)
            return 1
        print("stale_last_event_id", "gone+op08_hint")

        print("S14_SMOKE_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("SMOKE_FAIL", type(exc).__name__, exc, file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            _stop_uvicorn(proc)
            print("uvicorn_stopped")
        if inv_id:
            try:
                _cleanup_investigation(database_url, inv_id)
                print("cleanup_investigation", inv_id)
            except Exception as exc:  # noqa: BLE001
                print("cleanup_warn", inv_id, exc, file=sys.stderr)
        try:
            adapter_cleanup = RedisWakeAdapter(redis_url=redis_url)
            adapter_cleanup.flush_wakeup_queue()
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
