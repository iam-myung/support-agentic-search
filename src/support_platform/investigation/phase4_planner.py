"""Phase4 sequential tool planner: local_retrieve → web_search → Chat synthesize.

Used by official conversation entry when real adapters are wired.
Public outputs never include CoT / prompt / api_key.
"""

from __future__ import annotations

from typing import Any

_NON_LOCAL_MARKERS = (
    "非基于本地知识",
    "不是基于本地知识",
    "非本地知识库",
    "不基于本地知识库",
    "来自网络",
    "网络来源",
)


class Phase4SequentialPlanner:
    """Fixed tool sequence + real Chat synthesize (one planner instance per question)."""

    def __init__(self, *, chat: Any, question: str) -> None:
        self.chat = chat
        self.question = question
        self._actions = [
            {"type": "tool", "tool": "local_retrieve", "query": question},
            {"type": "tool", "tool": "web_search", "query": question},
            {"type": "synthesize"},
        ]

    def next_action(self, state: dict[str, Any]) -> dict[str, Any]:
        _ = state
        if not self._actions:
            return {"type": "synthesize"}
        return dict(self._actions.pop(0))

    def synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        local_hits = list(state.get("local_hits") or [])
        web_results = list(state.get("web_results") or [])
        web_snippets: list[dict[str, str]] = []
        for result in web_results:
            hits = getattr(result, "hits", None) or []
            for hit in hits[:3]:
                web_snippets.append(
                    {
                        "title": str(getattr(hit, "title", "") or ""),
                        "url": str(getattr(hit, "url", "") or ""),
                        "snippet": str(getattr(hit, "snippet", "") or "")[:300],
                    }
                )
        base = self.chat.synthesize(
            question=self.question,
            hits=local_hits,
            assessment={"next_action": "sufficient", "gaps": []},
            result_status="ANSWERED",
        )
        summary = str(base.get("summary") or "").strip()
        used_web = bool(web_snippets)
        used_local = bool(local_hits)
        if used_web and used_local:
            knowledge_basis = "MIXED"
        elif used_web:
            knowledge_basis = "WEB"
        else:
            knowledge_basis = "LOCAL"
        if knowledge_basis in {"WEB", "MIXED"} and not any(
            m in summary for m in _NON_LOCAL_MARKERS
        ):
            summary = (summary + " 本结论部分或全部非基于本地知识库。").strip()
        claims: list[dict[str, str]] = []
        if used_local and local_hits:
            claims.append(
                {
                    "text": str(getattr(local_hits[0], "content", "") or "")[:120]
                    or "本地证据",
                    "provenance": "LOCAL",
                }
            )
        if used_web and web_snippets:
            claims.append(
                {
                    "text": web_snippets[0].get("title") or "网络来源",
                    "provenance": "WEB",
                }
            )
        return {
            "result_status": str(base.get("result_status") or "ANSWERED"),
            "summary": summary,
            "knowledge_basis": knowledge_basis,
            "claims": claims,
        }
