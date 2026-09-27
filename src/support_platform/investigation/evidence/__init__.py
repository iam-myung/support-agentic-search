"""Claim-evidence validation and result-status adjudication (SPEC §3/§5)."""

from __future__ import annotations

from typing import Any

INVESTIGATION_RULES_FINGERPRINT = "inv-rules-v1-no-tools-no-auto-reply"

_FACT = "FACT"
_CASE_REF = "CASE_REFERENCE"
_SUPPORTS = "SUPPORTS"
_CONTRADICTS = "CONTRADICTS"


def apply_untrusted_context(state: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """Treat corpus/user text as data only — never mutate rules, tools, or auto-reply."""
    _ = texts  # untrusted; intentionally unused for control-plane fields
    out = dict(state)
    out["rules_fingerprint"] = state.get("rules_fingerprint") or INVESTIGATION_RULES_FINGERPRINT
    out["tool_allowlist"] = list(state.get("tool_allowlist") or [])
    out["auto_customer_reply"] = bool(state.get("auto_customer_reply"))
    # Public log stays free of secrets / instructional payloads.
    out["public_log"] = state.get("public_log") or ""
    return out


def freeze_evidence_snapshots(
    evidence: list[dict[str, Any]],
    *,
    snapshot_chunk_ids: frozenset[str] | set[str],
) -> list[dict[str, Any]]:
    """Materialize quote/locator/version snapshots for in-snapshot rows only."""
    frozen: list[dict[str, Any]] = []
    snap = set(snapshot_chunk_ids)
    for row in evidence:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id not in snap:
            continue
        item = dict(row)
        item["quote_snapshot"] = row.get("quote_snapshot") or row.get("quote") or ""
        item["locator_snapshot"] = row.get("locator_snapshot") or row.get("locator")
        item["source_title_snapshot"] = row.get("source_title_snapshot") or row.get("source_title")
        item["version_snapshot"] = row.get("version_snapshot") or row.get("version_id")
        frozen.append(item)
    return frozen


def _is_historical(ev: dict[str, Any]) -> bool:
    if ev.get("is_historical_case"):
        return True
    locator = ev.get("locator") or ev.get("locator_snapshot") or {}
    if isinstance(locator, dict) and locator.get("kind") == "case_ref":
        return True
    return False


def _evidence_by_id(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(e["id"]): e for e in evidence if e.get("id") is not None}


def validate_claim_evidence(
    *,
    draft: dict[str, Any],
    evidence: list[dict[str, Any]],
    snapshot_chunk_ids: frozenset[str] | set[str],
    high_risk: bool = False,
) -> dict[str, Any]:
    """Adjudicate draft claims against snapshot-scoped evidence; never run tools or auto-reply."""
    snap = set(snapshot_chunk_ids)
    by_id = _evidence_by_id(evidence)

    # Drop / ignore citations whose chunk is outside the investigation freeze.
    in_snap_evidence = freeze_evidence_snapshots(evidence, snapshot_chunk_ids=snap)
    in_snap_ids = {str(e["id"]) for e in in_snap_evidence}

    conflicting = [str(x) for x in list(draft.get("conflicting_ids") or [])]
    has_contradict = any(
        str(e.get("relation") or "").upper() == _CONTRADICTS for e in in_snap_evidence
    )
    risk = bool(high_risk or draft.get("high_risk"))
    missing_ctx = list(draft.get("missing_required_context") or [])

    base = {
        "tools_executed": [],
        "auto_customer_reply_sent": False,
        "warnings": [],
    }

    if risk or missing_ctx:
        return {
            **base,
            "result_status": "NEEDS_HUMAN",
            "summary": "",
            "claims": [],
            "evidence": in_snap_evidence,
        }

    if conflicting or has_contradict:
        # Keep both sides visible; do not silently pick a winner.
        both_ids = set(conflicting) | {
            str(e["id"])
            for e in evidence
            if str(e.get("relation") or "").upper() in {_SUPPORTS, _CONTRADICTS}
        }
        shown = [dict(e) for e in evidence if str(e.get("id")) in both_ids] or list(evidence)
        return {
            **base,
            "result_status": "CONFLICT",
            "summary": "",
            "claims": [],
            "evidence": shown,
        }

    # Rewrite claims: strip out-of-snapshot ids; demote historical-only support to CASE_REFERENCE.
    rewritten: list[dict[str, Any]] = []
    for claim in list(draft.get("claims") or []):
        raw_ids = [str(i) for i in list(claim.get("evidence_ids") or [])]
        valid_ids = [i for i in raw_ids if i in in_snap_ids]
        if not valid_ids:
            continue
        hist_ids = [i for i in valid_ids if _is_historical(by_id[i])]
        non_hist = [i for i in valid_ids if i not in hist_ids]
        ctype = str(claim.get("type") or _FACT)
        if ctype == _FACT and not non_hist:
            # Historical case alone cannot ground a current FACT.
            rewritten.append(
                {
                    "text": claim.get("text") or "",
                    "type": _CASE_REF,
                    "evidence_ids": hist_ids,
                }
            )
            continue
        if ctype == _FACT and non_hist:
            # FACT requires at least one SUPPORTS among non-historical in-snap evidence.
            supports = [
                i
                for i in non_hist
                if str(by_id[i].get("relation") or "").upper() == _SUPPORTS
            ]
            if not supports:
                continue
            rewritten.append(
                {
                    "text": claim.get("text") or "",
                    "type": _FACT,
                    "evidence_ids": supports,
                }
            )
            continue
        rewritten.append(
            {
                "text": claim.get("text") or "",
                "type": ctype if ctype else _CASE_REF,
                "evidence_ids": valid_ids,
            }
        )

    factual = [c for c in rewritten if c.get("type") == _FACT]
    case_refs = [c for c in rewritten if c.get("type") == _CASE_REF]

    if not factual:
        return {
            **base,
            "result_status": "NO_EVIDENCE",
            "summary": "",
            "claims": case_refs,
            "evidence": in_snap_evidence,
        }

    return {
        **base,
        "result_status": "ANSWERED",
        "summary": (draft.get("summary") or "").strip(),
        "claims": rewritten,
        "evidence": in_snap_evidence,
    }
