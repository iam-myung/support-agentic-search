"""Chat model port: Fake (tests) and HTTP OpenAI-compatible (SMOKE when approved)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class FakeChatAdapter:
    """Deterministic assess/rewrite/synthesize for unit tests (injected scripts)."""

    def __init__(
        self,
        *,
        assess_actions: list[str] | None = None,
        rewrite_to: str = "rewritten query",
        synthesize_summary: str = "根据已导入资料给出的处理建议。",
    ) -> None:
        self.assess_actions = assess_actions or ["sufficient"]
        self.rewrite_to = rewrite_to
        self.synthesize_summary = synthesize_summary
        self.assess_calls = 0
        self.rewrite_calls = 0
        self.synthesize_calls = 0

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        self.assess_calls += 1
        action = self.assess_actions[min(self.assess_calls - 1, len(self.assess_actions) - 1)]
        gaps = ["need additional source"] if action == "resolvable_gap" else []
        return {
            "next_action": action,
            "gaps": gaps,
            "candidate_claims": [],
            "supporting_ids": [getattr(h, "chunk_id", h) for h in hits],
            "conflicting_ids": [],
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        self.rewrite_calls += 1
        return self.rewrite_to

    def synthesize(
        self,
        *,
        question: str,
        hits: list[Any],
        assessment: dict[str, Any],
        result_status: str,
    ) -> dict[str, Any]:
        self.synthesize_calls += 1
        action = (assessment or {}).get("next_action")
        status = result_status
        if action == "sufficient":
            status = "ANSWERED"
        elif action == "conflict":
            status = "CONFLICT"
        summary = self.synthesize_summary
        if status == "CONFLICT" and "冲突" not in summary:
            summary = f"冲突：资料存在互斥结论。{summary}".strip()
        return {"result_status": status, "summary": summary, "claims": []}


class HttpChatAdapter:
    """Minimal OpenAI-compatible chat client for approved SMOKE only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def _complete(self, messages: list[dict[str, str]]) -> str:
        payload = json.dumps(
            {"model": self.model, "messages": messages, "temperature": 0}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:  # pragma: no cover
            raise RuntimeError(f"chat request failed: {exc}") from exc
        return str(body["choices"][0]["message"]["content"])

    def assess(self, *, question: str, hits: list[Any], public_only: bool = True) -> dict[str, Any]:
        snippets = [
            {"chunk_id": getattr(h, "chunk_id", ""), "content": getattr(h, "content", "")[:400]}
            for h in hits
        ]
        raw = self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only with keys next_action, gaps, supporting_ids. "
                        "next_action one of sufficient,resolvable_gap,no_evidence,conflict,human_needed. "
                        "Do not invent customer facts."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "hits": snippets}, ensure_ascii=False
                    ),
                },
            ]
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            data = json.loads(raw[start : end + 1]) if start >= 0 and end > start else {}
        return {
            "next_action": data.get("next_action", "no_evidence"),
            "gaps": list(data.get("gaps") or []),
            "candidate_claims": [],
            "supporting_ids": list(data.get("supporting_ids") or []),
            "conflicting_ids": [],
        }

    def rewrite(self, *, question: str, constraints: dict[str, Any], gaps: list[str]) -> str:
        raw = self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the search query using only the question, constraints, and gaps. "
                        "Do not add new customer facts. Reply with the query text only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "constraints": constraints, "gaps": gaps},
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        return raw.strip().strip('"')

    def synthesize(
        self,
        *,
        question: str,
        hits: list[Any],
        assessment: dict[str, Any],
        result_status: str,
    ) -> dict[str, Any]:
        snippets = [
            {"chunk_id": getattr(h, "chunk_id", ""), "content": getattr(h, "content", "")[:400]}
            for h in hits
        ]
        raw = self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only with keys result_status, summary. "
                        "summary must be non-empty Chinese/English advice for ANSWERED or CONFLICT; "
                        "for CONFLICT outline both sides. Never include chain-of-thought."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": question,
                            "result_status": result_status,
                            "assessment": {
                                k: assessment.get(k)
                                for k in ("next_action", "gaps", "supporting_ids", "conflicting_ids")
                            },
                            "hits": snippets,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            data = json.loads(raw[start : end + 1]) if start >= 0 and end > start else {}
        summary = str(data.get("summary") or raw or "").strip()
        status = str(data.get("result_status") or result_status)
        return {"result_status": status, "summary": summary, "claims": []}
