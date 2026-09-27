"""Phase 1 + Phase 2 evaluation: fixed dataset, baseline, AC-014 quality gates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

InvestigateFn = Callable[..., dict[str, Any]]

_COMPARE_KEYS = (
    "dataset_hash",
    "knowledge_snapshot_hash",
    "prompt_version",
    "model_config_version",
    "retrieval_config_version",
)

_AC014_MIN_CASES = 60
_AC014_MIN_FACTS = 200
_AC014_MAX_UNGROUNDED_RATE = 0.03
_SAFETY_STATUSES = frozenset({"NO_EVIDENCE", "CONFLICT", "NEEDS_HUMAN"})

DEFAULT_OUTPUT_DIR = Path("artifacts") / "evaluation"


def dataset_hash(dataset: list[dict[str, Any]]) -> str:
    """Stable hash of the annotated dataset for AC-007 traceability."""
    payload = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_evaluation(
    *,
    dataset: list[dict[str, Any]],
    investigate: InvestigateFn,
    baseline: bool = False,
    knowledge_snapshot_hash: str,
    prompt_version: str,
    model_config_version: str,
    retrieval_config_version: str,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run fixed cases through the Investigation Service and emit a report.

    Does not alter knowledge or investigation rules; only records outcomes.
    When ``baseline`` is True, the injectable service is told to use single-retrieval
    (no re-query) via the ``baseline`` keyword.
    """
    ds_hash = dataset_hash(dataset)
    manifest: dict[str, Any] = {
        "dataset_hash": ds_hash,
        "knowledge_snapshot_hash": knowledge_snapshot_hash,
        "prompt_version": prompt_version,
        "model_config_version": model_config_version,
        "retrieval_config_version": retrieval_config_version,
        "baseline": bool(baseline),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    cases: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    matched = 0

    for case in dataset:
        case_id = str(case["id"])
        expected = case.get("expected_status")
        result = investigate(
            question=str(case["question"]),
            context=case.get("context") or {},
            baseline=bool(baseline),
            case_id=case_id,
        )
        actual = result.get("result_status")
        row = {
            "id": case_id,
            "expected_status": expected,
            "actual_status": actual,
            "investigation_id": result.get("id"),
            "raw": result,
        }
        cases.append(row)
        if actual == expected:
            matched += 1
        else:
            failures.append(
                {
                    "id": case_id,
                    "expected_status": expected,
                    "actual_status": actual,
                    "reason": "status_mismatch",
                }
            )

    total = len(dataset) or 1
    report: dict[str, Any] = {
        "manifest": manifest,
        "metrics": {
            "status_accuracy": matched / total,
            "case_count": len(dataset),
            "failure_count": len(failures),
        },
        "cases": cases,
        "failures": failures,
    }

    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        mode = "baseline" if baseline else "primary"
        path = out / f"report_{mode}_{ds_hash[:12]}.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report["output_path"] = str(path)

    return report


def compare_to_baseline(
    *,
    primary: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Compare primary vs baseline reports.

    Relative improvement is only computed when knowledge/dataset/config hashes match
    (PRD §4.1). Divergent conditions → different-config trial, not a lift score.
    """
    p_m = primary.get("manifest") or {}
    b_m = baseline.get("manifest") or {}
    mismatches = [
        key for key in _COMPARE_KEYS if p_m.get(key) != b_m.get(key)
    ]
    # baseline flag itself is expected to differ; exclude from mismatch list logic above
    # (baseline key is not in _COMPARE_KEYS)

    if mismatches:
        return {
            "comparable": False,
            "reason": "inconsistent_baseline_conditions",
            "mismatched_keys": mismatches,
            "relative_improvement": None,
        }

    p_acc = float((primary.get("metrics") or {}).get("status_accuracy") or 0.0)
    b_acc = float((baseline.get("metrics") or {}).get("status_accuracy") or 0.0)
    return {
        "comparable": True,
        "reason": "matched_conditions",
        "relative_improvement": p_acc - b_acc,
        "primary_status_accuracy": p_acc,
        "baseline_status_accuracy": b_acc,
    }


def compute_ungrounded_fact_rate(
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """PRD §4.2 / SPEC §7: unsupported ÷ ALL generated facts (no sampling)."""
    fact_count = len(facts)
    if fact_count == 0:
        return {
            "unsupported_count": 0,
            "fact_count": 0,
            "ungrounded_fact_rate": None,
            "rate_status": "N/A",
            "sampled": False,
            "denominator_mode": "all_facts",
        }

    unsupported = sum(1 for row in facts if not bool(row.get("supported")))
    return {
        "unsupported_count": unsupported,
        "fact_count": fact_count,
        "ungrounded_fact_rate": unsupported / fact_count,
        "rate_status": "ok",
        "sampled": False,
        "denominator_mode": "all_facts",
    }


def build_phase2_quality_report(
    *,
    cases: list[dict[str, Any]],
    knowledge_snapshot_hash: str,
    prompt_version: str,
    model_config_version: str,
    retrieval_config_version: str,
    provider_mode: str = "fake",
    baseline_answerable_coverage: float | None = None,
    annotation_scope: str = "all_facts",
) -> dict[str, Any]:
    """Assemble Phase 2 quality report with safety failures and fact metrics."""
    failures: list[dict[str, Any]] = []
    all_facts: list[dict[str, Any]] = []
    answerable_total = 0
    answerable_hit = 0
    safety_ok = True

    for case in cases:
        case_id = str(case.get("id") or "")
        expected = case.get("expected_status")
        actual = case.get("actual_status")
        facts = list(case.get("facts") or [])
        all_facts.extend(facts)

        if actual != expected:
            failures.append(
                {
                    "id": case_id,
                    "expected_status": expected,
                    "actual_status": actual,
                    "reason": "status_mismatch",
                    "category": case.get("category"),
                }
            )

        is_safety = (
            expected in _SAFETY_STATUSES
            or str(case.get("category") or "")
            in {"no_evidence", "conflict", "needs_human"}
        )
        if is_safety and actual != expected:
            safety_ok = False

        if bool(case.get("answerable")):
            answerable_total += 1
            if actual == "ANSWERED" and actual == expected:
                answerable_hit += 1

    rate_info = compute_ungrounded_fact_rate(all_facts)
    coverage = (
        (answerable_hit / answerable_total) if answerable_total else None
    )
    meets_baseline: bool | None = None
    if (
        coverage is not None
        and baseline_answerable_coverage is not None
    ):
        meets_baseline = coverage >= float(baseline_answerable_coverage)

    return {
        "phase": 2,
        "prd_section": "4.2",
        "manifest": {
            "knowledge_snapshot_hash": knowledge_snapshot_hash,
            "prompt_version": prompt_version,
            "model_config_version": model_config_version,
            "retrieval_config_version": retrieval_config_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "metrics": {
            "case_count": len(cases),
            "fact_count": rate_info["fact_count"],
            "unsupported_count": rate_info["unsupported_count"],
            "ungrounded_fact_rate": rate_info["ungrounded_fact_rate"],
            "answerable_coverage": coverage,
            "meets_baseline_coverage": meets_baseline,
            "safety_pass": safety_ok,
            "failure_count": len(failures),
        },
        "cases": cases,
        "failures": failures,
        "annotation_scope": annotation_scope,
        "provider_mode": provider_mode,
        "fact_annotations": all_facts,
    }


def compare_answerable_coverage(
    *,
    primary: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Compare answerable coverage only under matched knowledge/config hashes."""
    p_m = primary.get("manifest") or {}
    b_m = baseline.get("manifest") or {}
    mismatches = [key for key in _COMPARE_KEYS if p_m.get(key) != b_m.get(key)]
    if mismatches:
        return {
            "comparable": False,
            "reason": "inconsistent_baseline_conditions",
            "mismatched_keys": mismatches,
            "meets_baseline": False,
        }

    p_cov = float((primary.get("metrics") or {}).get("answerable_coverage") or 0.0)
    b_cov = float((baseline.get("metrics") or {}).get("answerable_coverage") or 0.0)
    return {
        "comparable": True,
        "reason": "matched_conditions",
        "primary_answerable_coverage": p_cov,
        "baseline_answerable_coverage": b_cov,
        "meets_baseline": p_cov >= b_cov,
    }


def assert_ac014_claim_allowed(report: dict[str, Any]) -> dict[str, Any]:
    """Gate AC-014 ≤3% claims: sample floors, full annotation, safety, provider.

    Insufficient sample, pass-only gaming, or Fake provider must not yield a
    real-effect AC-014 PASS (PRD §4.2 / SPEC §7).
    """
    metrics = report.get("metrics") or {}
    provider_mode = str(report.get("provider_mode") or "fake")
    scope = str(report.get("annotation_scope") or "all_facts")
    case_count = int(metrics.get("case_count") or 0)
    fact_count = int(metrics.get("fact_count") or 0)
    rate = metrics.get("ungrounded_fact_rate")
    safety_pass = metrics.get("safety_pass") is True
    meets_baseline = metrics.get("meets_baseline_coverage", True) is True

    decision: dict[str, Any] = {
        "provider_mode": provider_mode,
        "claim_eligible": False,
        "passed_ac014": False,
        "passed_ac014_real": False,
        "real_effect_claim": False,
        "real_effect_blocked": False,
        "reason": "",
    }

    if case_count < _AC014_MIN_CASES or fact_count < _AC014_MIN_FACTS:
        decision["reason"] = "insufficient_sample"
        return decision

    if scope in {"pass_only", "cherry_pick", "subset"}:
        decision["reason"] = "pass_only_annotation_gaming"
        return decision
    if scope not in {"all_facts", "full"}:
        decision["reason"] = "incomplete_annotation_scope"
        return decision

    # Sample floors + full annotation → formula claim eligibility.
    decision["claim_eligible"] = True

    if not safety_pass:
        decision["reason"] = "safety_failed"
        if provider_mode == "fake":
            decision["real_effect_blocked"] = True
        return decision

    rate_ok = isinstance(rate, (int, float)) and float(rate) <= _AC014_MAX_UNGROUNDED_RATE
    if not rate_ok:
        decision["reason"] = "rate_above_threshold_or_na"
        if provider_mode == "fake":
            decision["real_effect_blocked"] = True
        return decision

    if not meets_baseline:
        decision["reason"] = "below_baseline_coverage"
        if provider_mode == "fake":
            decision["real_effect_blocked"] = True
        return decision

    if provider_mode == "fake":
        decision["reason"] = "fake_provider_blocks_real_effect"
        decision["real_effect_blocked"] = True
        decision["passed_ac014"] = False
        decision["passed_ac014_real"] = False
        decision["real_effect_claim"] = False
        return decision

    decision["reason"] = "ac014_gates_passed"
    decision["passed_ac014"] = True
    decision["passed_ac014_real"] = True
    decision["real_effect_claim"] = True
    return decision


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "assert_ac014_claim_allowed",
    "build_phase2_quality_report",
    "compare_answerable_coverage",
    "compare_to_baseline",
    "compute_ungrounded_fact_rate",
    "dataset_hash",
    "run_evaluation",
]
