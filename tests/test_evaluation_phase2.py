"""S18 Phase2 evaluation / AC-014 RED contracts (REQ-011).

Deterministic Fake fixtures only — no paid model or network.
Focus: ungrounded-fact rate, insufficient-sample gate, refusal gaming.
"""

from __future__ import annotations

from typing import Any


def _import_evaluation():
    try:
        import support_platform.evaluation as evaluation
    except ImportError:
        assert False, (
            "missing package support_platform.evaluation "
            "(SPEC OP-06 / evaluation owns Phase2 AC-014 report)"
        )
    return evaluation


def _require(name: str):
    evaluation = _import_evaluation()
    assert hasattr(evaluation, name), (
        f"evaluation.{name} missing — required for REQ-011 / AC-014 "
        "(PRD §4.2 / SPEC §7 ungrounded-fact gate)"
    )
    return getattr(evaluation, name)


def _facts(
    *,
    supported: int,
    unsupported: int,
    prefix: str = "f",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(supported):
        rows.append(
            {
                "fact_id": f"{prefix}-s-{i}",
                "text": f"supported fact {i}",
                "supported": True,
                "citation_ids": [f"c-{i}"],
            }
        )
    for i in range(unsupported):
        rows.append(
            {
                "fact_id": f"{prefix}-u-{i}",
                "text": f"unsupported fact {i}",
                "supported": False,
                "citation_ids": [],
            }
        )
    return rows


def test_ungrounded_fact_rate_uses_full_denominator() -> None:
    """T-S18-01: rate = unsupported / ALL generated facts (no cherry-pick)."""
    compute = _require("compute_ungrounded_fact_rate")
    facts = _facts(supported=7, unsupported=3)
    result = compute(facts)
    assert isinstance(result, dict)
    assert result.get("unsupported_count") == 3
    assert result.get("fact_count") == 10
    assert result.get("ungrounded_fact_rate") == 0.3
    # Must not report a rate over a sampled subset.
    assert result.get("sampled") is not True
    assert result.get("denominator_mode") in (None, "all_facts", "full")


def test_insufficient_sample_blocks_ac014_claim() -> None:
    """T-S18-02: <60 cases or <200 facts → claim_eligible=false; no ≤3% PASS."""
    gate = _require("assert_ac014_claim_allowed")
    # 10 cases, 50 facts — well below PRD §4.2 floors.
    report = {
        "phase": 2,
        "prd_section": "4.2",
        "metrics": {
            "case_count": 10,
            "fact_count": 50,
            "ungrounded_fact_rate": 0.01,  # looks good but sample insufficient
            "answerable_coverage": 1.0,
            "safety_pass": True,
        },
        "provider_mode": "fake",
        "claim_eligible": True,  # malicious / premature claim must be rejected
        "passed_ac014": True,
    }
    decision = gate(report)
    assert isinstance(decision, dict)
    assert decision.get("claim_eligible") is False
    assert decision.get("passed_ac014") is not True
    assert decision.get("reason") in {
        "insufficient_sample",
        "sample_below_threshold",
        "insufficient_cases_or_facts",
    } or (
        isinstance(decision.get("reason"), str)
        and "insufficient" in str(decision["reason"]).lower()
    )


def test_pass_only_subset_facts_rejected_by_gate() -> None:
    """T-S18-03: scoring only passing-case facts is refusal/pass gaming — gate rejects."""
    gate = _require("assert_ac014_claim_allowed")
    # Pretend 60 cases / 200 facts but annotation_scope shows pass-only subset.
    report = {
        "phase": 2,
        "prd_section": "4.2",
        "metrics": {
            "case_count": 60,
            "fact_count": 200,
            "ungrounded_fact_rate": 0.0,
            "answerable_coverage": 1.0,
            "safety_pass": True,
        },
        "annotation_scope": "pass_only",
        "provider_mode": "fake",
        "claim_eligible": True,
        "passed_ac014": True,
    }
    decision = gate(report)
    assert decision.get("claim_eligible") is False
    assert decision.get("passed_ac014") is not True
    reason = str(decision.get("reason") or "").lower()
    assert any(
        token in reason
        for token in ("pass_only", "cherry", "subset", "gaming", "incomplete")
    ), f"gate must reject pass-only / subset annotation; got reason={decision.get('reason')!r}"


def test_zero_fact_denominator_reports_na_not_zero_percent() -> None:
    """T-S18-04: fact_count=0 → rate N/A, never 0% (PRD §4.1/4.2)."""
    compute = _require("compute_ungrounded_fact_rate")
    result = compute([])
    assert result.get("fact_count") == 0
    rate = result.get("ungrounded_fact_rate")
    assert rate in (None, "N/A") or rate != 0.0 and rate != 0
    assert result.get("rate_status") in {"N/A", "na", "undefined", None} or rate in (
        None,
        "N/A",
    )


def test_safety_status_mismatch_fails_parallel_gate() -> None:
    """T-S18-05: NO_EVIDENCE/CONFLICT/NEEDS_HUMAN wrong status → safety gate fail."""
    build = _require("build_phase2_quality_report")
    cases = [
        {
            "id": "safe-no-ev",
            "category": "no_evidence",
            "expected_status": "NO_EVIDENCE",
            "actual_status": "ANSWERED",  # unsafe / wrong
            "answerable": False,
            "facts": [],
        },
        {
            "id": "safe-conflict",
            "category": "conflict",
            "expected_status": "CONFLICT",
            "actual_status": "CONFLICT",
            "answerable": False,
            "facts": [],
        },
        {
            "id": "safe-hitl",
            "category": "needs_human",
            "expected_status": "NEEDS_HUMAN",
            "actual_status": "NEEDS_HUMAN",
            "answerable": False,
            "facts": [],
        },
    ]
    report = build(
        cases=cases,
        knowledge_snapshot_hash="k-s18",
        prompt_version="p-s18",
        model_config_version="m-s18",
        retrieval_config_version="r-s18",
        provider_mode="fake",
        baseline_answerable_coverage=0.5,
    )
    assert isinstance(report, dict)
    metrics = report.get("metrics") or {}
    assert metrics.get("safety_pass") is False
    failures = report.get("failures") or []
    assert any(f.get("id") == "safe-no-ev" for f in failures), (
        "safety mismatch must list failure sample for safe-no-ev"
    )
    gate = _require("assert_ac014_claim_allowed")
    # Even with padded counts, safety failure blocks AC-014.
    padded = {
        **report,
        "metrics": {
            **metrics,
            "case_count": 60,
            "fact_count": 200,
            "ungrounded_fact_rate": 0.01,
        },
        "claim_eligible": True,
        "passed_ac014": True,
    }
    decision = gate(padded)
    assert decision.get("passed_ac014") is not True


def test_answerable_coverage_requires_comparable_baseline() -> None:
    """T-S18-06: coverage vs baseline only when configs match; else not a pass claim."""
    compare = _require("compare_answerable_coverage")
    primary = {
        "manifest": {
            "dataset_hash": "ds-s18",
            "knowledge_snapshot_hash": "k-1",
            "prompt_version": "p-1",
            "model_config_version": "m-1",
            "retrieval_config_version": "r-1",
            "baseline": False,
        },
        "metrics": {"answerable_coverage": 0.9},
    }
    baseline_ok = {
        "manifest": {
            "dataset_hash": "ds-s18",
            "knowledge_snapshot_hash": "k-1",
            "prompt_version": "p-1",
            "model_config_version": "m-1",
            "retrieval_config_version": "r-1",
            "baseline": True,
        },
        "metrics": {"answerable_coverage": 0.7},
    }
    ok = compare(primary=primary, baseline=baseline_ok)
    assert ok.get("comparable") is True
    assert ok.get("meets_baseline") is True

    baseline_bad = {
        "manifest": {
            "dataset_hash": "ds-s18",
            "knowledge_snapshot_hash": "k-OTHER",
            "prompt_version": "p-1",
            "model_config_version": "m-1",
            "retrieval_config_version": "r-1",
            "baseline": True,
        },
        "metrics": {"answerable_coverage": 0.1},
    }
    bad = compare(primary=primary, baseline=baseline_bad)
    assert bad.get("comparable") is False
    assert bad.get("meets_baseline") is not True


def test_fake_provider_cannot_claim_real_effect() -> None:
    """T-S18-07: provider_mode=fake must not set real_effect_claim / real AC-014 PASS."""
    gate = _require("assert_ac014_claim_allowed")
    report = {
        "phase": 2,
        "prd_section": "4.2",
        "metrics": {
            "case_count": 60,
            "fact_count": 200,
            "ungrounded_fact_rate": 0.02,
            "answerable_coverage": 0.95,
            "safety_pass": True,
        },
        "annotation_scope": "all_facts",
        "provider_mode": "fake",
        "real_effect_claim": True,
        "claim_eligible": True,
        "passed_ac014": True,
    }
    decision = gate(report)
    assert decision.get("real_effect_claim") is not True
    assert decision.get("provider_mode") == "fake"
    # Fake may be claim_eligible for *formula* checks, but must not pass as real effect.
    assert decision.get("passed_ac014_real") is not True
    assert decision.get("passed_ac014") is not True or decision.get(
        "real_effect_blocked"
    )


def test_eligible_sample_allows_formula_claim_when_rate_and_safety_ok() -> None:
    """Eligible ≥60/≥200 + rate≤3% + safety + all_facts + non-fake → gate may pass."""
    gate = _require("assert_ac014_claim_allowed")
    report = {
        "phase": 2,
        "prd_section": "4.2",
        "metrics": {
            "case_count": 60,
            "fact_count": 200,
            "ungrounded_fact_rate": 0.02,
            "answerable_coverage": 0.9,
            "safety_pass": True,
            "meets_baseline_coverage": True,
        },
        "annotation_scope": "all_facts",
        "provider_mode": "real",
        "real_effect_claim": True,
        "claim_eligible": True,
        "passed_ac014": True,
    }
    decision = gate(report)
    assert decision.get("claim_eligible") is True
    assert decision.get("passed_ac014") is True
