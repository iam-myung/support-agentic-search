"""S25 Phase4 chat UI RED contracts (REQ-014 / AC-018；AC-021/022 UI disclosure).

SPEC §4 OP-14～17 + §8 S25:
- GET / serves conversation UI (not form workbench as default main entry)
- POST / form workbench path removed or 302 → chat
- One round: ask → visible public steps → suggestion
- WEB/MIXED knowledge_basis + non-local disclosure rendered; claims LOCAL/WEB
- No api_key / cot / prompt leakage in HTML or static assets
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "src" / "support_platform" / "static"
_TEMPLATES = _REPO / "src" / "support_platform" / "templates"
_FORBIDDEN = ("prompt", "system", "cot", "api_key", "internal")
_NON_LOCAL_MARKERS = (
    "非基于本地知识",
    "不是基于本地知识",
    "非本地知识库",
    "不基于本地知识库",
    "来自网络",
    "网络来源",
)


def _client() -> TestClient:
    from support_platform.main import app

    return TestClient(app)


def test_ac018_root_get_is_conversation_ui_not_form_workbench() -> None:
    """AC-018: official GET / opens chat UI; form workbench is not the default main entry."""
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    ctype = (response.headers.get("content-type") or "").lower()
    assert "text/html" in ctype
    body = response.text

    chat_markers = (
        "data-chat" in body
        or 'id="chat"' in body
        or 'id="messages"' in body
        or "data-conversation" in body
        or "对话" in body
    )
    assert chat_markers, "GET / must expose a conversation/chat UI surface"

    assert "<h1>客服工作台</h1>" not in body, (
        "legacy workbench H1 must not be the official root entry"
    )
    assert 'id="investigate-form"' not in body, (
        "legacy investigate-form must not be default root UI"
    )
    assert 'method="post" action="/"' not in body.replace("'", '"'), (
        "root must not POST the legacy workbench form to /"
    )

    assert (
        'id="chat-input"' in body
        or 'name="content"' in body
        or "data-chat-input" in body
        or "placeholder=" in body
    ), "chat composer/input missing"


def test_ac018_root_post_no_longer_runs_legacy_workbench_main_path() -> None:
    """AC-018: POST / must not succeed as the old form workbench investigation path."""
    client = _client()
    try:
        response = client.post(
            "/",
            data={"question": "How do I reset my password?", "error_code": "E1001"},
            follow_redirects=False,
        )
    except RuntimeError as exc:
        # Legacy handler still bound: it tries to run investigation instead of 302→chat.
        raise AssertionError(
            "POST / still routes to legacy workbench investigation handler "
            f"(expected 302/405 to chat): {exc}"
        ) from exc
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location") or ""
        assert location.startswith("/") or "chat" in location.lower()
        return
    if response.status_code in {404, 405, 410}:
        return
    assert response.status_code == 200
    body = response.text
    assert "<h1>客服工作台</h1>" not in body
    assert "ANSWERED" not in body or "data-chat" in body or 'id="messages"' in body


def test_ac018_one_round_ask_shows_public_steps_and_suggestion() -> None:
    """AC-018: at least one round ask → visible process → suggestion via conversation API + UI."""
    client = _client()

    created = client.post("/api/v2/conversations", json={"title": "s25-red"})
    assert created.status_code == 201, (
        f"OP-15 missing/failed: status={created.status_code} body={created.text[:200]}"
    )
    payload = created.json()
    conversation_id = payload.get("conversation_id") or payload.get("id")
    assert conversation_id, f"missing conversation_id: {payload}"

    msg = client.post(
        f"/api/v2/conversations/{conversation_id}/messages",
        json={"content": "How do I reset my password?"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert msg.status_code in {200, 202}, (
        f"OP-16 missing/failed: status={msg.status_code} body={msg.text[:300]}"
    )

    detail = client.get(f"/api/v2/conversations/{conversation_id}")
    assert detail.status_code == 200, (
        f"OP-17 missing/failed: status={detail.status_code} body={detail.text[:300]}"
    )
    body = detail.json()
    steps = body.get("public_steps")
    if steps is None:
        for m in body.get("messages") or []:
            if isinstance(m, dict) and m.get("public_steps"):
                steps = m.get("public_steps")
                break
    assert isinstance(steps, list) and len(steps) >= 1, f"public_steps missing: {body}"

    summary = body.get("summary")
    output = body.get("output") if isinstance(body.get("output"), dict) else {}
    if not summary:
        summary = output.get("summary")
    if not summary:
        for m in body.get("messages") or []:
            if isinstance(m, dict) and m.get("role") == "assistant":
                summary = m.get("content") or (m.get("output") or {}).get("summary")
                break
    assert isinstance(summary, str) and summary.strip(), f"suggestion/summary missing: {body}"

    page = client.get("/").text
    assert (
        "public_steps" in page
        or "data-public-steps" in page
        or 'id="steps"' in page
        or "过程" in page
        or "步骤" in page
    )
    assert (
        "suggestion" in page
        or "data-suggestion" in page
        or 'id="suggestion"' in page
        or "建议" in page
        or "summary" in page
    )


def test_ac021_ui_shows_web_knowledge_basis_and_non_local_declaration() -> None:
    """AC-021 UI: WEB basis + explicit non-local disclosure must be renderable in chat shell."""
    templates = list(_TEMPLATES.glob("*.html"))
    static_js = list(_STATIC.glob("*.js"))
    assert templates, "templates/ missing"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in templates + static_js)
    assert "knowledge_basis" in blob, "chat UI must reference knowledge_basis for disclosure"
    assert (
        any(m in blob for m in _NON_LOCAL_MARKERS)
        or "nonLocal" in blob
        or "non_local" in blob
        or "disclosure" in blob
        or "来源披露" in blob
    ), "chat UI must include non-local disclosure copy or rendering hook"

    page = _client().get("/")
    assert page.status_code == 200
    html = page.text
    assert (
        "knowledge_basis" in html
        or "data-knowledge-basis" in html
        or "WEB" in html
        or "来源" in html
    ), "root chat page must expose knowledge basis / source disclosure surface"


def test_ac022_ui_distinguishes_local_and_web_claims() -> None:
    """AC-022 UI: MIXED claims must be distinguishable as LOCAL vs WEB in markup/JS."""
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in list(_TEMPLATES.glob("*.html")) + list(_STATIC.glob("*.js"))
    )
    assert "LOCAL" in blob and "WEB" in blob, (
        "chat UI assets must mention LOCAL and WEB provenance labels"
    )
    assert "provenance" in blob or "claim" in blob.lower() or "claims" in blob, (
        "chat UI must render claim provenance"
    )


def test_chat_ui_forbids_secret_and_cot_fields_in_chat_assets() -> None:
    """Security: chat templates/static must not embed cot/prompt/api_key secrets."""
    files = [
        p
        for p in list(_TEMPLATES.glob("*.html")) + list(_STATIC.glob("*.js"))
        if "chat" in p.name.lower() or "conversation" in p.name.lower()
    ]
    assert files, "expected chat/conversation templates or static assets"
    for path in files:
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        assert "secret_chain_of_thought" not in lower
        assert "tvly-" not in lower
        for key in _FORBIDDEN:
            assert f"step.{key}" not in text
            assert f'step["{key}"]' not in text
            assert f"step['{key}']" not in text


def test_chat_static_assets_linked_from_root() -> None:
    """Chat page must link dedicated static CSS/JS (not only legacy workbench assets)."""
    client = _client()
    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "/static/" in html
    linked_chat = (
        "chat.css" in html
        or "chat.js" in html
        or "conversation.css" in html
        or "conversation.js" in html
    )
    assert linked_chat, "root chat UI must link chat/conversation static assets"
    for name in ("chat.css", "chat.js", "conversation.css", "conversation.js"):
        if name in html:
            asset = client.get(f"/static/{name}")
            assert asset.status_code == 200, f"missing static asset /static/{name}"
