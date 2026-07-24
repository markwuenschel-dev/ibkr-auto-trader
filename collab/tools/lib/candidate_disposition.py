"""Evidence-complete candidate disposition records with acceptance guardrails."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import collab_common as cc
import run_evidence

SCHEMA_VERSION = "1.0"
DISPOSITIONS = {"accepted", "conditional", "rejected", "repair_required", "superseded", "blocked"}
REJECTION_REASONS = {
    "tests_failed",
    "build_failed",
    "type_checking_failed",
    "lint_failed",
    "contract_violation",
    "incomplete_implementation",
    "regression_introduced",
    "duplicate_candidate",
    "lower_quality_than_selected",
    "unsafe_change",
    "excessive_scope",
    "unsupported_assumption",
    "missing_runtime_proof",
    "telemetry_gap",
    "evaluator_disagreement",
    "invalid_or_malformed_response",
    "timeout_or_interrupted_generation",
}


class CandidateDispositionError(cc.CollabError):
    """A disposition is incomplete or conflicts with immutable evidence."""


def _path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events.jsonl"


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise CandidateDispositionError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(item, dict):
            raise CandidateDispositionError("run-events.jsonl contains a non-object record")
        out.append(item)
    return out


def _append(collab: str | Path, event: dict[str, Any]) -> None:
    try:
        run_evidence.append(collab, event, identity_field="event_id")
    except run_evidence.RunEvidenceError as exc:
        raise CandidateDispositionError(str(exc)) from exc


def record_disposition(
    collab: str | Path,
    *,
    run_uid: str,
    candidate_id: str,
    handoff_id: str,
    timestamp: str,
    disposition: str,
    executive_explanation: str,
    evidence_refs: tuple[str, ...],
    impact: str,
    remediation: str,
    retryable: bool,
    final: bool,
    retained_work: tuple[str, ...],
    alternatives: tuple[str, ...],
    weaknesses: tuple[str, ...],
    unavailable_evidence: tuple[str, ...],
    confidence: str,
    disagreements: tuple[dict[str, Any], ...],
    resolution: str,
    human_review_triggers: tuple[str, ...],
    requirements_evaluation: dict[str, Any] | None = None,
    decision_maker: str = "candidate-assessment",
    primary_reason: str | None = None,
    reason_categories: tuple[str, ...] = (),
    failed_checks: tuple[str, ...] = (),
    round_number: int | None = None,
    superseded_by_candidate_id: str | None = None,
) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise CandidateDispositionError(f"unknown disposition: {disposition}")
    if not all((run_uid, candidate_id, handoff_id, timestamp, executive_explanation, impact, remediation)):
        raise CandidateDispositionError("disposition identity and explanation fields are required")
    evaluation = requirements_evaluation or {}
    if disposition == "accepted":
        if not evaluation.get("eligible"):
            raise CandidateDispositionError("accepted disposition requires complete critical requirements")
        if evaluation.get("oracle_status") != "passed" or not evaluation.get("oracle_validation_id"):
            raise CandidateDispositionError("accepted disposition requires a named passing oracle")
    default_reasons = (primary_reason,) if primary_reason else ()
    normalized_reasons = tuple(dict.fromkeys(reason_categories or default_reasons))
    if any(reason not in REJECTION_REASONS for reason in normalized_reasons):
        raise CandidateDispositionError("rejection reason categories must use the typed vocabulary")
    if disposition == "rejected" and (
        primary_reason not in REJECTION_REASONS or primary_reason not in normalized_reasons
    ):
        raise CandidateDispositionError("rejected disposition requires a typed rejection reason")
    if disposition == "superseded" and not str(superseded_by_candidate_id or "").strip():
        raise CandidateDispositionError("superseded disposition requires a successor candidate")
    if superseded_by_candidate_id == candidate_id:
        raise CandidateDispositionError("a candidate cannot supersede itself")
    if not decision_maker.strip():
        raise CandidateDispositionError("decision_maker is required")
    identity = f"{run_uid}:{candidate_id}:{disposition}:{timestamp}"
    event = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "candidate_disposition",
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "collab-disposition:" + identity)),
        "run_uid": run_uid,
        "candidate_id": candidate_id,
        "handoff_id": handoff_id,
        "timestamp": timestamp,
        "disposition": disposition,
        "executive_explanation": executive_explanation,
        "evidence_refs": list(evidence_refs),
        "impact": impact,
        "remediation": remediation,
        "retryable": bool(retryable),
        "final": bool(final),
        "retained_work": list(retained_work),
        "alternatives": list(alternatives),
        "weaknesses": list(weaknesses),
        "unavailable_evidence": list(unavailable_evidence),
        "confidence": confidence,
        "disagreements": [dict(item) for item in disagreements],
        "resolution": resolution,
        "human_review_triggers": list(human_review_triggers),
        "requirements_evaluation": evaluation,
        "acceptance_oracle": evaluation.get("oracle_validation_id"),
        "decision_maker": decision_maker,
        "primary_reason": primary_reason,
        "reason_categories": list(normalized_reasons),
        "failed_checks": list(failed_checks),
        "round_number": round_number,
        "superseded_by_candidate_id": superseded_by_candidate_id,
    }
    _append(collab, event)
    return event


def read_dispositions(collab: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return read_disposition_file(_path(collab), run_uid=run_uid)


def read_disposition_file(path: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in _records(Path(path))
        if item.get("record_type") == "candidate_disposition"
        and (run_uid is None or item.get("run_uid") == run_uid)
    ]
