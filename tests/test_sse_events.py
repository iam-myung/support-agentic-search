"""S14 SSE events / Last-Event-ID catch-up RED (REQ-008 / AC-010).

Focus (SPEC §4 OP-09; §8 S14):
- per-task sequence strictly increasing + unique
- Last-Event-ID reconnect returns only newer events
- terminal SSE event consistent with OP-08 fact
- public payload must not leak reasoning/secrets/unauthorized sources
- ANSWER_CHUNK only after claim/evidence validation
- stale Last-Event-ID must prompt re-read of OP-08 (not silent empty completeness)
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

EVENT_TYPES = frozenset(
    {
        "QUEUED",
        "STARTED",
        "STEP",
        "REVIEW_REQUIRED",
        "ANSWER_CHUNK",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
)

FORBIDDEN_PAYLOAD_MARKERS = (
    "chain of thought",
    "internal reasoning",
    "system prompt",
    "api_key",
    "sk-",
    "LLM_API_KEY",
    "raw_retrieval",
    "unauthorized_source",
)


def _load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "DATABASE_URL":
                return value.strip().strip('"').strip("'")
    raise AssertionError("DATABASE_URL must be set for S14 SSE contracts")


def _fresh_app_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", _load_database_url())
    import support_platform.config as config_mod
    import support_platform.main as main_mod

    importlib.reload(config_mod)
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def _login(client: TestClient, *, username: str) -> tuple[dict[str, str], str]:
    from support_platform.auth.testing import ensure_user, login_session

    ensure_user(username=username, password="secret-ok", role="SUPPORT_AGENT")
    return login_session(client, username=username, password="secret-ok")


def _create_queued(
    client: TestClient,
    *,
    cookies: dict[str, str],
    csrf: str,
    question: str,
) -> str:
    resp = client.post(
        "/api/v2/investigations",
        json={"question": question, "context": {}},
        cookies=cookies,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"s14-create-{uuid4()}"},
    )
    assert resp.status_code == 202, f"OP-07 seed failed: {resp.status_code} {resp.text}"
    return str(resp.json()["id"])


def _events_api():
    try:
        from support_platform.task_runtime import events as ev

        return ev
    except ImportError as exc:
        raise AssertionError(
            "support_platform.task_runtime.events must exist (S14 event adapter)"
        ) from exc


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse a finite SSE buffer into {id, event, data} frames."""
    frames: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    data_lines: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            if current or data_lines:
                payload = "\n".join(data_lines)
                try:
                    current["data"] = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    current["data"] = payload
                frames.append(current)
                current = {}
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
        else:
            field, value = line, ""
        if field == "id":
            current["id"] = value
        elif field == "event":
            current["event"] = value
        elif field == "data":
            data_lines.append(value)
    if current or data_lines:
        payload = "\n".join(data_lines)
        try:
            current["data"] = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            current["data"] = payload
        frames.append(current)
    return frames


def _seed_event(
    *,
    investigation_id: str,
    sequence: int,
    event_type: str,
    public_payload: dict[str, Any],
) -> None:
    """Fixture insert into investigation_events (GREEN may replace via append_event)."""
    engine = create_engine(_load_database_url(), pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO investigation_events (
                  id, investigation_id, sequence, type, public_payload
                ) VALUES (
                  CAST(:eid AS uuid), CAST(:iid AS uuid), :seq, :typ,
                  CAST(:payload AS jsonb)
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "eid": str(uuid4()),
                "iid": investigation_id,
                "seq": sequence,
                "typ": event_type,
                "payload": json.dumps(public_payload, ensure_ascii=False),
            },
        )


def _mark_completed(*, investigation_id: str, result_status: str = "ANSWERED") -> None:
    engine = create_engine(_load_database_url(), pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE investigations
                SET task_status = 'COMPLETED',
                    result_status = :rs,
                    output = CAST(:out AS jsonb),
                    public_steps = CAST(:steps AS jsonb)
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {
                "id": investigation_id,
                "rs": result_status,
                "out": json.dumps(
                    {"summary": "validated reply", "suggested_reply": "Please try again."},
                    ensure_ascii=False,
                ),
                "steps": json.dumps(
                    [{"kind": "retrieve_1", "hit_count": 1}],
                    ensure_ascii=False,
                ),
            },
        )


# --- Auth / visibility ---


def test_op09_unauthenticated_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_app_client(monkeypatch)
    resp = client.get(f"/api/v2/investigations/{uuid4()}/events")
    assert resp.status_code == 401, (
        f"OP-09 unauthenticated must 401, got {resp.status_code} {resp.text}"
    )


def test_op09_invisible_investigation_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fresh_app_client(monkeypatch)
    cookies_o, csrf_o = _login(owner, username="s14-owner")
    inv_id = _create_queued(
        owner, cookies=cookies_o, csrf=csrf_o, question="S14-RED owner only"
    )
    # Owner must reach OP-09 (proves route exists — missing route also 404s).
    owned = owner.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies_o,
        headers={"Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert owned.status_code == 200, (
        f"owner OP-09 must 200 (route present), got {owned.status_code} {owned.text}"
    )

    stranger = _fresh_app_client(monkeypatch)
    cookies_s, _csrf_s = _login(stranger, username="s14-stranger")
    resp = stranger.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies_s,
        headers={"Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert resp.status_code == 404, (
        f"OP-09 invisible investigation must 404, got {resp.status_code}"
    )


# --- Event adapter / sequence ---


def test_append_event_assigns_strictly_increasing_sequences() -> None:
    ev = _events_api()
    append = getattr(ev, "append_event", None)
    assert callable(append), "events.append_event required"

    from support_platform.task_runtime import create_queued_investigation

    database_url = _load_database_url()
    iid = uuid4()
    create_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question="S14-RED sequence",
        context={},
        owner_user_id=uuid4(),
    )
    # OP-07 already wrote QUEUED sequence=1; append more.
    a = append(
        database_url=database_url,
        investigation_id=iid,
        event_type="STARTED",
        public_payload={"task_status": "RUNNING"},
    )
    b = append(
        database_url=database_url,
        investigation_id=iid,
        event_type="STEP",
        public_payload={"kind": "retrieve_1", "hit_count": 2},
    )
    c = append(
        database_url=database_url,
        investigation_id=iid,
        event_type="COMPLETED",
        public_payload={"task_status": "COMPLETED", "result_status": "ANSWERED"},
    )
    seqs = [int(a["sequence"]), int(b["sequence"]), int(c["sequence"])]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3, f"sequences must strictly increase: {seqs}"
    assert seqs[0] >= 2, f"appended events must continue after QUEUED seed, got {seqs}"
    for row in (a, b, c):
        assert row["type"] in EVENT_TYPES


def test_list_events_after_last_event_id_returns_only_newer() -> None:
    ev = _events_api()
    list_after = getattr(ev, "list_events_after", None) or getattr(ev, "list_after", None)
    assert callable(list_after), "events.list_events_after required for catch-up"

    from support_platform.task_runtime import create_queued_investigation

    database_url = _load_database_url()
    iid = str(uuid4())
    create_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question="S14-RED catch-up",
        context={},
        owner_user_id=uuid4(),
    )
    _seed_event(
        investigation_id=iid,
        sequence=2,
        event_type="STARTED",
        public_payload={"task_status": "RUNNING"},
    )
    _seed_event(
        investigation_id=iid,
        sequence=3,
        event_type="STEP",
        public_payload={"kind": "assess"},
    )
    _seed_event(
        investigation_id=iid,
        sequence=4,
        event_type="COMPLETED",
        public_payload={"task_status": "COMPLETED"},
    )

    newer = list_after(
        database_url=database_url,
        investigation_id=iid,
        last_event_id=2,
    )
    assert newer, "catch-up after Last-Event-ID=2 must return later events"
    seqs = [int(e["sequence"]) for e in newer]
    assert all(s > 2 for s in seqs), f"must only return sequence>2, got {seqs}"
    assert 3 in seqs and 4 in seqs


# --- OP-09 SSE HTTP ---


def test_op09_sse_content_type_and_last_event_id_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s14-sse-a")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S14-RED SSE catch-up"
    )
    _seed_event(
        investigation_id=inv_id,
        sequence=2,
        event_type="STARTED",
        public_payload={"task_status": "RUNNING"},
    )
    _seed_event(
        investigation_id=inv_id,
        sequence=3,
        event_type="STEP",
        public_payload={"kind": "retrieve_1", "hit_count": 1},
    )
    _seed_event(
        investigation_id=inv_id,
        sequence=4,
        event_type="COMPLETED",
        public_payload={"task_status": "COMPLETED", "result_status": "ANSWERED"},
    )
    _mark_completed(investigation_id=inv_id)

    resp = client.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies,
        headers={"Last-Event-ID": "2", "Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert resp.status_code == 200, (
        f"OP-09 must 200 for owner, got {resp.status_code} {resp.text}"
    )
    ctype = (resp.headers.get("content-type") or "").lower()
    assert "text/event-stream" in ctype, f"OP-09 content-type must be SSE, got {ctype}"

    frames = _parse_sse(resp.text)
    assert frames, "SSE body must contain at least one event frame after Last-Event-ID=2"
    ids = []
    for fr in frames:
        if fr.get("event") in {"error", "gone", "reload"}:
            continue
        eid = fr.get("id")
        if eid is not None and str(eid).isdigit():
            ids.append(int(eid))
    assert ids, f"SSE frames must carry numeric id=sequence, got {frames!r}"
    assert all(i > 2 for i in ids), f"catch-up must omit <=2, got {ids}"


def test_op09_terminal_event_matches_op08_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s14-sse-b")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S14-RED terminal consistency"
    )
    _seed_event(
        investigation_id=inv_id,
        sequence=2,
        event_type="COMPLETED",
        public_payload={"task_status": "COMPLETED", "result_status": "ANSWERED"},
    )
    _mark_completed(investigation_id=inv_id, result_status="ANSWERED")

    sse = client.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies,
        headers={"Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert sse.status_code == 200, sse.text
    frames = _parse_sse(sse.text)
    terminal = [f for f in frames if f.get("event") in {"COMPLETED", "FAILED", "CANCELLED"}]
    assert terminal, f"SSE must include a terminal event, frames={frames!r}"

    fact = client.get(f"/api/v2/investigations/{inv_id}", cookies=cookies)
    assert fact.status_code == 200, fact.text
    body = fact.json()
    last = terminal[-1]
    data = last.get("data") if isinstance(last.get("data"), dict) else {}
    assert body.get("task_status") == "COMPLETED"
    assert data.get("task_status") == body.get("task_status"), (
        f"terminal SSE task_status must match OP-08; sse={data} op08={body}"
    )
    if "result_status" in data:
        assert data.get("result_status") == body.get("result_status")


def test_op09_payload_omits_sensitive_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s14-sse-c")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S14-RED no secret leak"
    )
    ev = _events_api()
    append = getattr(ev, "append_event", None)
    assert callable(append), "append_event required"
    append(
        database_url=_load_database_url(),
        investigation_id=inv_id,
        event_type="STEP",
        public_payload={
            "kind": "retrieve_1",
            "hit_count": 1,
            "api_key": "sk-secret-should-never-stream",
            "internal_reasoning": "chain of thought hidden",
            "raw_retrieval": ["doc dump"],
            "unauthorized_source": "private-wiki",
        },
    )

    resp = client.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies,
        headers={"Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert resp.status_code == 200, resp.text
    blob = resp.text.lower()
    for marker in FORBIDDEN_PAYLOAD_MARKERS:
        assert marker.lower() not in blob, (
            f"SSE must not contain sensitive marker {marker!r}; body excerpt={resp.text[:400]!r}"
        )


def test_answer_chunk_rejected_without_validation_gate() -> None:
    """ANSWER_CHUNK must not be appendable for unverified draft text."""
    ev = _events_api()
    append = getattr(ev, "append_event", None)
    assert callable(append), "append_event required"
    gate = getattr(ev, "append_answer_chunk", None) or getattr(
        ev, "emit_answer_chunk", None
    )
    assert callable(gate), (
        "events.append_answer_chunk (or emit_answer_chunk) required — "
        "ANSWER_CHUNK must go through validation gate, not raw append"
    )

    from support_platform.task_runtime import create_queued_investigation

    database_url = _load_database_url()
    iid = uuid4()
    create_queued_investigation(
        database_url=database_url,
        investigation_id=iid,
        question="S14-RED answer chunk gate",
        context={},
        owner_user_id=uuid4(),
    )

    with pytest.raises(Exception) as raised:
        gate(
            database_url=database_url,
            investigation_id=iid,
            chunk_text="Unverified model draft about refunds",
            validated=False,
            result_status=None,
            evidence_ids=[],
        )
    assert raised.value is not None

    ok = gate(
        database_url=database_url,
        investigation_id=iid,
        chunk_text="Please reset password from Settings.",
        validated=True,
        result_status="ANSWERED",
        evidence_ids=["chunk-1"],
    )
    assert ok.get("type") == "ANSWER_CHUNK" or ok.get("event_type") == "ANSWER_CHUNK"
    assert int(ok.get("sequence") or 0) >= 2


def test_stale_last_event_id_prompts_op08_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Last-Event-ID is far beyond retained sequences, client must be told to re-read OP-08."""
    client = _fresh_app_client(monkeypatch)
    cookies, csrf = _login(client, username="s14-sse-d")
    inv_id = _create_queued(
        client, cookies=cookies, csrf=csrf, question="S14-RED stale Last-Event-ID"
    )
    resp = client.get(
        f"/api/v2/investigations/{inv_id}/events",
        cookies=cookies,
        headers={"Last-Event-ID": "999", "Accept": "text/event-stream"},
        params={"snapshot": "1"},
    )
    assert resp.status_code in {200, 409, 410}, (
        f"stale Last-Event-ID must not 500; got {resp.status_code}"
    )
    text_body = resp.text.lower()
    frames = _parse_sse(resp.text) if resp.status_code == 200 else []
    control = " ".join(
        str(f.get("event", "")) + " " + json.dumps(f.get("data"), ensure_ascii=False)
        for f in frames
    ).lower()
    combined = text_body + " " + control
    assert (
        "op-08" in combined
        or "op08" in combined
        or "/investigations/" in combined
        or "reload" in combined
        or "gone" in combined
        or "stale" in combined
        or "re-read" in combined
        or "reread" in combined
    ), (
        "stale Last-Event-ID must prompt re-read of OP-08 / reload; "
        f"got status={resp.status_code} body={resp.text[:500]!r}"
    )


def test_sanitize_public_payload_helper_exists() -> None:
    ev = _events_api()
    sanitize = getattr(ev, "sanitize_public_payload", None) or getattr(
        ev, "public_payload_only", None
    )
    assert callable(sanitize), "sanitize_public_payload required"
    cleaned = sanitize(
        {
            "kind": "step",
            "hit_count": 2,
            "api_key": "sk-abc",
            "internal_reasoning": "secret cot",
            "source_count": 2,
        }
    )
    assert isinstance(cleaned, dict)
    blob = json.dumps(cleaned).lower()
    assert "sk-abc" not in blob and "secret cot" not in blob
    assert cleaned.get("kind") == "step" or cleaned.get("hit_count") == 2
