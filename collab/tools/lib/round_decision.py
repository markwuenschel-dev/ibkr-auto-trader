"""Deterministic, append-only continue/stop decisions for explicit round boundaries."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import collab_common as cc
import run_evidence

SCHEMA_VERSION = "1.0"

# Evaluation order is authoritative. Operational/safety failures precede ordinary policy completion,
# which is why a dependency timeout at a nominal cap is not mislabeled as cap exhaustion.
STOP_REASONS = (
    "user_cancellation",
    "policy_or_safety_rejection",
    "orchestration_error",
    "required_dependency_unavailable",
    "timeout_reached",
    "model_failure_threshold_reached",
    "accepted_result_met_completion_criteria",
    "evaluator_judged_further_rounds_unnecessary",
    "no_viable_candidates_remained",
    "all_remaining_candidates_rejected",
    "duplicate_or_non_improving_output",
    "convergence_threshold_reached",
    "budget_exhausted",
    "round_cap_reached",
)


class RoundDecisionError(cc.CollabError):
    """A decision record is invalid or collides with immutable evidence."""


@dataclass(frozen=True)
class DecisionContext:
    run_uid: str
    completed_round: int
    maximum_rounds: int
    candidate_id: str | None = None
    unresolved_requirements: tuple[str, ...] = ()
    viable_models: tuple[str, ...] = ()
    remaining_budget: dict[str, Any] | None = None
    timestamp: str = ""
    supporting_evidence: tuple[str, ...] = ()
    evaluator_version: str = "round-decision-v1"
    viable_candidates: int = 1
    accepted: bool = False
    completion_criteria_met: bool = False
    further_rounds_unnecessary: bool = False
    all_remaining_candidates_rejected: bool = False
    duplicate_or_non_improving: bool = False
    convergence_threshold_reached: bool = False
    budget_exhausted: bool = False
    timeout_reached: bool = False
    user_cancelled: bool = False
    required_dependency_unavailable: bool = False
    model_failure_threshold_reached: bool = False
    policy_or_safety_rejection: bool = False
    orchestration_error: bool = False

    def __post_init__(self) -> None:
        if not self.run_uid or self.completed_round < 0 or self.maximum_rounds < 0:
            raise RoundDecisionError("run_uid and non-negative round counts are required")
        if self.completed_round > self.maximum_rounds and self.maximum_rounds:
            raise RoundDecisionError("completed_round cannot exceed maximum_rounds")


def _rule_results(context: DecisionContext) -> dict[str, tuple[bool, dict[str, Any]]]:
    accepted = bool(
        context.accepted
        and context.completion_criteria_met
        and not context.unresolved_requirements
    )
    return {
        "user_cancellation": (context.user_cancelled, {"user_cancelled": context.user_cancelled}),
        "policy_or_safety_rejection": (
            context.policy_or_safety_rejection,
            {"policy_or_safety_rejection": context.policy_or_safety_rejection},
        ),
        "orchestration_error": (
            context.orchestration_error,
            {"orchestration_error": context.orchestration_error},
        ),
        "required_dependency_unavailable": (
            context.required_dependency_unavailable,
            {"required_dependency_unavailable": context.required_dependency_unavailable},
        ),
        "timeout_reached": (
            context.timeout_reached,
            {"timeout_reached": context.timeout_reached},
        ),
        "model_failure_threshold_reached": (
            context.model_failure_threshold_reached,
            {"model_failure_threshold_reached": context.model_failure_threshold_reached},
        ),
        "budget_exhausted": (
            context.budget_exhausted,
            {"budget_exhausted": context.budget_exhausted},
        ),
        "round_cap_reached": (
            bool(context.maximum_rounds and context.completed_round >= context.maximum_rounds),
            {
                "completed_round": context.completed_round,
                "maximum_rounds": context.maximum_rounds,
            },
        ),
        "accepted_result_met_completion_criteria": (
            accepted,
            {
                "accepted": context.accepted,
                "completion_criteria_met": context.completion_criteria_met,
                "unresolved_requirement_count": len(context.unresolved_requirements),
            },
        ),
        "evaluator_judged_further_rounds_unnecessary": (
            context.further_rounds_unnecessary,
            {"further_rounds_unnecessary": context.further_rounds_unnecessary},
        ),
        "no_viable_candidates_remained": (
            context.viable_candidates <= 0,
            {"viable_candidate_count": context.viable_candidates},
        ),
        "all_remaining_candidates_rejected": (
            context.all_remaining_candidates_rejected,
            {"all_remaining_candidates_rejected": context.all_remaining_candidates_rejected},
        ),
        "duplicate_or_non_improving_output": (
            context.duplicate_or_non_improving,
            {"duplicate_or_non_improving": context.duplicate_or_non_improving},
        ),
        "convergence_threshold_reached": (
            context.convergence_threshold_reached,
            {"convergence_threshold_reached": context.convergence_threshold_reached},
        ),
    }


def evaluate(context: DecisionContext) -> dict[str, Any]:
    results = _rule_results(context)
    reason = next((name for name in STOP_REASONS if results[name][0]), None)
    decision = "stop" if reason else "continue"
    selected_reason = reason or "unresolved_requirements_and_viable_path"
    identity = {
        "run_uid": context.run_uid,
        "completed_round": context.completed_round,
        "candidate_id": context.candidate_id,
        "decision": decision,
        "reason": selected_reason,
        "supporting_evidence": list(context.supporting_evidence),
    }
    decision_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "collab-round-decision:"
            + json.dumps(identity, sort_keys=True, separators=(",", ":")),
        )
    )
    rules = [
        {"rule": name, "inputs": results[name][1], "result": results[name][0]}
        for name in STOP_REASONS
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "round_decision",
        "decision_id": decision_id,
        "run_uid": context.run_uid,
        "completed_round": context.completed_round,
        "maximum_rounds": context.maximum_rounds,
        "decision": decision,
        "reason": selected_reason,
        "rules_evaluated": rules,
        "decision_maker_component": "autopilot.round_decision",
        "evaluator_version": context.evaluator_version,
        "candidate_id": context.candidate_id,
        "unresolved_requirements": list(context.unresolved_requirements),
        "remaining_viable_models": list(context.viable_models),
        "remaining_budget": context.remaining_budget or {
            "tokens": None,
            "time_seconds": None,
            "cost": None,
        },
        "timestamp": context.timestamp,
        "supporting_evidence": list(context.supporting_evidence),
    }


def _path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events.jsonl"


def read_decisions(collab: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return read_decision_file(_path(collab), run_uid=run_uid)


def read_decision_file(path: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise RoundDecisionError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(item, dict):
            raise RoundDecisionError("run-events.jsonl contains a non-object record")
        if item.get("record_type") != "round_decision":
            continue
        if run_uid is None or item.get("run_uid") == run_uid:
            records.append(item)
    return records


def persist(collab: str | Path, decision: dict[str, Any]) -> None:
    if decision.get("record_type") != "round_decision" or not decision.get("decision_id"):
        raise RoundDecisionError("a typed decision_id is required")
    try:
        run_evidence.append(collab, decision, identity_field="decision_id")
    except run_evidence.RunEvidenceError as exc:
        raise RoundDecisionError(str(exc)) from exc
