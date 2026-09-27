"""Generate frozen Phase-1 30-case eval dataset (PRD §4.1 mix)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "fixtures" / "eval" / "phase1_30.json"


def case(
    cid: str,
    category: str,
    question: str,
    expected_status: str,
    *,
    required_evidence: list[str] | None = None,
    forbidden_facts: list[str] | None = None,
    needs_human: bool = False,
    context: dict | None = None,
) -> dict:
    return {
        "id": cid,
        "category": category,
        "question": question,
        "context": context or {},
        "expected_status": expected_status,
        "required_evidence": required_evidence or [],
        "forbidden_facts": forbidden_facts or [],
        "needs_human": needs_human,
    }


def build() -> list[dict]:
    cases: list[dict] = []

    # direct_answer × 6
    for i, q in enumerate(
        [
            "How do I reset my password according to the product guide?",
            "What does the product guide say about contacting support?",
            "Where is the password reset procedure documented?",
            "Summarize the password reset steps from the imported guide.",
            "According to the guide, what should I do if the issue persists?",
            "What support contact guidance exists in the knowledge base?",
        ],
        start=1,
    ):
        cases.append(
            case(
                f"da-{i:02d}",
                "direct_answer",
                q,
                "ANSWERED",
                required_evidence=["guide"],
                forbidden_facts=["admin backdoor"],
            )
        )

    # multi_source_requery × 6
    for i, q in enumerate(
        [
            "Explain password reset and also what error E1001 means.",
            "I need MFA policy and the cache-cold error code explanation together.",
            "Combine guide reset steps with FAQ injection-safe guidance.",
            "What are reset steps and when is a live ledger check required?",
            "Describe E1001 and the refund high-risk rule in one investigation.",
            "How do reset steps and MFA conflict policies relate for one account?",
        ],
        start=1,
    ):
        cases.append(
            case(
                f"ms-{i:02d}",
                "multi_source_requery",
                q,
                "ANSWERED",
                required_evidence=["guide", "second-source"],
            )
        )

    # error_code_proper_noun × 4
    for i, q in enumerate(
        [
            "What does error code E1001 mean?",
            "My logs show E1001 — what is the documented meaning?",
            "Is E1001 related to a cold cache?",
            "Define the proprietary error E1001 from the knowledge base.",
        ],
        start=1,
    ):
        cases.append(
            case(
                f"ec-{i:02d}",
                "error_code",
                q,
                "ANSWERED",
                required_evidence=["E1001"],
            )
        )

    # diagnostic × 6
    for i, q in enumerate(
        [
            "Users cannot log in after password reset — how should I diagnose?",
            "Cache looks stale and login fails; what checks does the guide imply?",
            "Symptom: intermittent auth failure after reset. What evidence to gather?",
            "Customer sees E1001 during login; propose a diagnostic path.",
            "Reset email arrived but login still fails — next diagnostic steps?",
            "Account type enterprise, product_version 2.1, login loop after reset — diagnose.",
        ],
        start=1,
    ):
        ctx = {"product_version": "2.1", "account_type": "enterprise"} if i == 6 else {}
        cases.append(
            case(
                f"dg-{i:02d}",
                "diagnostic",
                q,
                "ANSWERED",
                required_evidence=["guide"],
                context=ctx,
            )
        )

    # no_evidence × 4
    for i, q in enumerate(
        [
            "What is the unpublished beta SKU price for Project Zephyr?",
            "List the secret launch date of internal SKU ZX-9000.",
            "What is the unpublished discount code for VIP-ONLY-2026?",
            "How many seats does the never-imported Contoso exclusive plan include?",
        ],
        start=1,
    ):
        cases.append(
            case(
                f"ne-{i:02d}",
                "no_evidence",
                q,
                "NO_EVIDENCE",
                forbidden_facts=["$999", "ZX-9000-SECRET"],
            )
        )

    # conflict × 4
    for i, q in enumerate(
        [
            "Does password reset require MFA? Sources may disagree.",
            "Must email MFA complete before password reset finishes?",
            "Is MFA mandatory or never required for password reset?",
            "Reconcile conflicting MFA requirements for password reset.",
        ],
        start=1,
    ):
        cases.append(
            case(
                f"cf-{i:02d}",
                "conflict",
                q,
                "CONFLICT",
                required_evidence=["mfa-yes", "mfa-no"],
            )
        )

    assert len(cases) == 30, len(cases)

    # Boundary extras (not counted in the 30 mix but useful for AC-008 / import edges)
    # Keep exactly 30 per PRD primary set; boundaries covered by corpus import in smoke.
    return cases


def main() -> None:
    cases = build()
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    payload = {
        "version": "phase1-30-v1",
        "prd_section": "4.1",
        "counts": counts,
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT, "cases", len(cases), "counts", counts)


if __name__ == "__main__":
    main()
