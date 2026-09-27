"""S15 Phase-2 workbench UI RED (REQ-008/009 · AC-010/011 UI).

Focus (SPEC §8 S15; §4 public SSE payload):
- login controls for OP-12 (no auth bypass in UI)
- progress / SSE subscription markers + Last-Event-ID catch-up
- HITL SUPPLY/APPROVE/EDIT/REJECT (+ cancel) controls
- must not stream/render unverified draft answer chunks client-side
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "src" / "support_platform" / "static"
_TEMPLATES = _REPO / "src" / "support_platform" / "templates"


def _client() -> TestClient:
    from support_platform.main import app

    return TestClient(app)


def _workbench_html() -> str:
    client = _client()
    response = client.get("/workbench")
    assert response.status_code == 200
    ctype = (response.headers.get("content-type") or "").lower()
    assert "text/html" in ctype
    return response.text


def test_workbench_exposes_login_form_for_op12() -> None:
    """Pilot UI must expose username/password login affordance (OP-12)."""
    body = _workbench_html()
    login_markers = (
        'data-auth-login',
        'id="login-form"',
        'name="username"',
        'name="password"',
        "/api/v2/auth/login",
    )
    assert any(m in body for m in login_markers), (
        "workbench must include a login form or OP-12 login endpoint hook"
    )
    # Password field required when login UI is present.
    assert 'type="password"' in body or "type='password'" in body


def test_workbench_exposes_progress_and_sse_subscription_markers() -> None:
    """Progress panel must declare SSE events URL for OP-09 (AC-010)."""
    body = _workbench_html()
    progress_markers = (
        "data-sse",
        "data-progress",
        "/api/v2/investigations/",
        "events",
        "EventSource",
        "text/event-stream",
    )
    assert any(m in body for m in progress_markers), (
        "workbench must expose SSE/progress subscription markers"
    )
    # Combined: either data attribute or explicit events path pattern in page/script.
    combined = body
    js_path = _STATIC / "workbench.js"
    if js_path.is_file():
        combined = body + "\n" + js_path.read_text(encoding="utf-8")
    assert "/events" in combined or "EventSource" in combined, (
        "SSE client must reference /events or EventSource"
    )


def test_workbench_declares_last_event_id_reconnect_behavior() -> None:
    """Reconnect/catch-up must reference Last-Event-ID (AC-010)."""
    body = _workbench_html()
    sources = [body]
    js_path = _STATIC / "workbench.js"
    assert js_path.is_file(), "static/workbench.js must exist for Phase-2 progress client"
    sources.append(js_path.read_text(encoding="utf-8"))
    blob = "\n".join(sources)
    markers = (
        "Last-Event-ID",
        "last-event-id",
        "lastEventId",
        "last_event_id",
    )
    assert any(m in blob for m in markers), (
        "UI/JS must declare Last-Event-ID reconnect / catch-up behavior"
    )


def test_workbench_exposes_hitl_action_controls() -> None:
    """HITL panel must offer SUPPLY/APPROVE/EDIT/REJECT and cancel (AC-011)."""
    body = _workbench_html()
    sources = [body]
    js_path = _STATIC / "workbench.js"
    if js_path.is_file():
        sources.append(js_path.read_text(encoding="utf-8"))
    blob = "\n".join(sources)

    for action in ("SUPPLY", "APPROVE", "EDIT", "REJECT"):
        assert action in blob, f"HITL control missing action {action}"

    cancel_markers = ("cancel", "CANCEL", "/cancel", "data-hitl-cancel")
    assert any(m in blob for m in cancel_markers), "HITL cancel control missing"

    resume_markers = ("/resume", "data-hitl", "REVIEW_REQUIRED", "INTERRUPTED")
    assert any(m in blob for m in resume_markers), (
        "HITL UI must reference resume / REVIEW_REQUIRED / interrupt state"
    )


def test_workbench_does_not_stream_unverified_draft_answers() -> None:
    """Client must not invent draft streaming; ANSWER_CHUNK only from validated SSE."""
    body = _workbench_html()
    sources = [body]
    js_path = _STATIC / "workbench.js"
    if js_path.is_file():
        sources.append(js_path.read_text(encoding="utf-8"))
    tpl = _TEMPLATES / "workbench.html"
    if tpl.is_file():
        sources.append(tpl.read_text(encoding="utf-8"))
    blob = "\n".join(sources).lower()

    forbidden_draft_hooks = (
        "unverified draft",
        "stream_draft",
        "raw_model_stream",
        "fake_typewriter_before_validation",
        "pre_validation_chunk",
    )
    for hook in forbidden_draft_hooks:
        assert hook not in blob, f"forbidden unverified-draft hook present: {hook}"

    # Positive contract: ANSWER_CHUNK handling must be gated on event type name.
    assert "answer_chunk" in blob or "ANSWER_CHUNK" in "\n".join(sources), (
        "client must handle ANSWER_CHUNK as a named SSE event type only"
    )


def test_workbench_phase2_static_js_is_linked_and_served() -> None:
    """Phase-2 client script must be linked from HTML and served as static asset."""
    body = _workbench_html()
    assert "workbench.js" in body, "workbench.html must link static/workbench.js"

    client = _client()
    js = client.get("/static/workbench.js")
    assert js.status_code == 200, "GET /static/workbench.js must return 200"
    ctype = (js.headers.get("content-type") or "").lower()
    assert (
        "javascript" in ctype
        or "ecmascript" in ctype
        or "text/plain" in ctype
        or js.text.strip() != ""
    )
