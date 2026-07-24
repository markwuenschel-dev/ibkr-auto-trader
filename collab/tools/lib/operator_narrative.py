"""Deterministic evidence-aware entries for the operator timeline."""

from __future__ import annotations

from typing import Any

import collab_common as cc

PRODUCER = "operator-narrative-v1"
_REQUIRED = (
    "actor",
    "action",
    "object",
    "reason",
    "consequence",
    "next_action",
    "operator_action",
    "stable_ids",
    "evidence_links",
    "source_type",
    "timestamp",
    "producer",
    "confidence",
)
_GENERIC = {"completed", "stopping condition met", "something happened", "done", "success"}


class OperatorNarrativeError(cc.CollabError):
    """A timeline entry lacks attributable, actionable meaning."""


def _plain(value: Any) -> str:
    return str(value or "unknown").replace("_", " ")


def validate(entry: dict[str, Any]) -> None:
    missing = [field for field in _REQUIRED if field not in entry]
    if missing:
        raise OperatorNarrativeError("missing narrative fields: " + ", ".join(missing))
    for field in ("actor", "action", "object", "reason", "consequence", "next_action", "timestamp"):
        value = str(entry.get(field) or "").strip()
        if not value or value.lower() in _GENERIC:
            raise OperatorNarrativeError(f"{field} is empty or generic")
    if not isinstance(entry.get("stable_ids"), dict) or not entry["stable_ids"]:
        raise OperatorNarrativeError("stable_ids must identify the projected fact")
    if not isinstance(entry.get("evidence_links"), list) or not entry["evidence_links"]:
        raise OperatorNarrativeError("at least one evidence link is required")


def from_round_decision(decision: dict[str, Any]) -> dict[str, Any]:
    choice = str(decision.get("decision") or "unknown")
    completed = decision.get("completed_round")
    maximum = decision.get("maximum_rounds")
    reason = _plain(decision.get("reason"))
    if choice == "stop":
        action = f"stopped after completed round {completed} of maximum {maximum}"
        consequence = "another round was not started"
        next_action = "wait for an operator decision or a newly eligible run"
    else:
        action = f"continued after completed round {completed} of maximum {maximum}"
        consequence = "another round became eligible to start"
        next_action = "dispatch the next eligible model attempt"
    operator_action = "none"
    if decision.get("reason") == "required_dependency_unavailable":
        operator_action = "restore the dependency or choose an explicit retry"
    elif decision.get("reason") == "user_cancellation":
        operator_action = "resume only when the user intends to continue"
    candidate_id = decision.get("candidate_id")
    entry = {
        "actor": "autopilot.round_decision",
        "action": action,
        "object": f"candidate {candidate_id}" if candidate_id else "the current run",
        "reason": reason,
        "consequence": consequence,
        "next_action": next_action,
        "operator_action": operator_action,
        "stable_ids": {
            "run_uid": decision.get("run_uid"),
            "candidate_id": candidate_id,
        },
        "evidence_links": list(decision.get("supporting_evidence") or [])
        or [f"decision:{decision.get('decision_id')}"],
        "source_type": "round_decision",
        "timestamp": decision.get("timestamp"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def from_model_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    state = str(attempt.get("state") or "unknown")
    model = str(attempt.get("requested_model") or "unknown model")
    failure = _plain(attempt.get("failure_classification"))
    if state == "completed":
        action = "model attempt completed"
        reason = "the provider returned a completed response"
        next_action = "evaluate the retained result"
    elif state in ("failed", "timed_out", "cancelled"):
        action = f"model attempt {state.replace('_', ' ')}"
        reason = failure
        next_action = "apply the persisted retry or stop policy"
    else:
        action = f"model attempt entered {state.replace('_', ' ')}"
        reason = "the retained lifecycle event advanced"
        next_action = "wait for the next lifecycle event"
    telemetry = attempt.get("telemetry_state")
    consequence = f"{model} execution is {state}"
    if telemetry == "telemetry_verified":
        consequence += " and telemetry was verified"
    elif telemetry == "telemetry_failed":
        consequence += " and telemetry verification failed"
        next_action = "reconcile the retained request against Langfuse"
    if telemetry == "telemetry_failed":
        operator_action = "inspect the telemetry failure and reconcile or retry export"
    elif state != "completed":
        operator_action = "inspect the failure and restore the dependency"
    else:
        operator_action = "none"
    entry = {
        "actor": str(attempt.get("seat") or "model runtime"),
        "action": action,
        "object": f"{model} attempt {attempt.get('attempt_id')}",
        "reason": reason,
        "consequence": consequence,
        "next_action": next_action,
        "operator_action": operator_action,
        "stable_ids": {
            "run_uid": attempt.get("run_uid"),
            "attempt_id": attempt.get("attempt_id"),
            "request_id": attempt.get("request_id"),
        },
        "evidence_links": [f"attempt:{attempt.get('attempt_id')}"],
        "source_type": "model_attempt",
        "timestamp": attempt.get("updated_ts") or attempt.get("started_ts"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def from_run_plan(plan: dict[str, Any]) -> dict[str, Any]:
    rounds = plan.get("rounds") or {}
    roster = plan.get("execution_roster") or []
    maximum = rounds.get("maximum")
    entry = {
        "actor": "autopilot.run_plan",
        "action": "run started with a declared plan",
        "object": str(plan.get("objective") or "the configured run objective"),
        "reason": str(
            plan.get("plain_language_strategy")
            or "the orchestration plan was declared before model dispatch"
        ),
        "consequence": f"up to {maximum} rounds and {len(roster)} configured roles became eligible",
        "next_action": "dispatch the first eligible model attempt",
        "operator_action": "none",
        "stable_ids": {"run_uid": plan.get("run_uid")},
        "evidence_links": ["run-plan.json"],
        "source_type": "run_plan",
        "timestamp": plan.get("created_ts"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def _producer_name(producer: Any) -> str:
    if not isinstance(producer, dict):
        return str(producer or "orchestration")
    role = str(producer.get("role") or "model")
    model = producer.get("model")
    return f"{role} ({model})" if model else role


def from_candidate_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "unknown")
    candidate_id = event.get("candidate_id")
    if event_type == "candidate_created":
        actor = _producer_name(event.get("producer"))
        action = "produced candidate"
        reason = "the assigned task produced a versioned change"
        consequence = f"candidate {candidate_id} became available for evaluation"
        next_action = "run the declared validations and evaluator review"
        evidence = [f"candidate:{candidate_id}"]
    else:
        actor = str(event.get("evaluator") or "candidate evaluator")
        outcome = _plain(event.get("outcome"))
        action = f"assessed candidate as {outcome}"
        feedback = [str(item) for item in event.get("feedback_refs") or []]
        reason = ", ".join(feedback) or "the evaluator recorded a sourced candidate outcome"
        consequence = f"candidate {candidate_id} is now {outcome} in retained history"
        next_action = (
            "record the final disposition and supporting oracle"
            if event.get("outcome") == "accepted"
            else "retain the rationale and apply the remediation policy"
        )
        evidence = feedback or [f"candidate:{candidate_id}"]
    entry = {
        "actor": actor,
        "action": action,
        "object": f"candidate {candidate_id}",
        "reason": reason,
        "consequence": consequence,
        "next_action": next_action,
        "operator_action": "none",
        "stable_ids": {
            "run_uid": event.get("run_uid"),
            "candidate_id": candidate_id,
            "handoff_id": event.get("handoff_id"),
        },
        "evidence_links": evidence,
        "source_type": "candidate_evidence",
        "timestamp": event.get("timestamp"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def from_validation(validation: dict[str, Any]) -> dict[str, Any]:
    status = _plain(validation.get("status"))
    candidate_id = validation.get("candidate_id")
    producer = str(validation.get("producer") or "validation producer")
    producer_version = str(validation.get("producer_version") or "version unknown")
    reason = f"{_plain(validation.get('source_kind'))} from {producer} {producer_version}"
    if validation.get("status") == "passed":
        consequence = f"candidate {candidate_id} gained supporting validation evidence"
        next_action = "continue evaluating remaining requirements"
        operator_action = "none"
    else:
        consequence = f"candidate {candidate_id} remains ineligible on this evidence"
        next_action = "inspect the failed checks and candidate remediation"
        operator_action = "review the failed evidence and remediation"
    artifact = str(validation.get("artifact_ref") or "validation artifact not recorded")
    entry = {
        "actor": producer,
        "action": f"validation {status}",
        "object": f"candidate {candidate_id} validation {validation.get('validation_id')}",
        "reason": reason,
        "consequence": consequence,
        "next_action": next_action,
        "operator_action": operator_action,
        "stable_ids": {
            "run_uid": validation.get("run_uid"),
            "candidate_id": candidate_id,
            "validation_id": validation.get("validation_id"),
        },
        "evidence_links": [artifact],
        "source_type": str(validation.get("source_kind") or "validation_evidence"),
        "timestamp": validation.get("timestamp"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def from_disposition(disposition: dict[str, Any]) -> dict[str, Any]:
    candidate_id = disposition.get("candidate_id")
    decision = _plain(disposition.get("disposition"))
    triggers = [str(item) for item in disposition.get("human_review_triggers") or []]
    entry = {
        "actor": str(disposition.get("decision_maker") or "candidate assessment"),
        "action": f"candidate {decision}",
        "object": f"candidate {candidate_id}",
        "reason": str(disposition.get("executive_explanation") or "disposition evidence recorded"),
        "consequence": str(disposition.get("impact") or "candidate eligibility changed"),
        "next_action": str(disposition.get("remediation") or "retain the decision evidence"),
        "operator_action": ", ".join(triggers) or "none",
        "stable_ids": {
            "run_uid": disposition.get("run_uid"),
            "candidate_id": candidate_id,
            "handoff_id": disposition.get("handoff_id"),
        },
        "evidence_links": list(disposition.get("evidence_refs") or [])
        or [f"candidate:{candidate_id}"],
        "source_type": "candidate_disposition",
        "timestamp": disposition.get("timestamp"),
        "producer": PRODUCER,
        "confidence": "verified_projection",
    }
    validate(entry)
    return entry


def project(
    *,
    attempts: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    candidate_events: list[dict[str, Any]] | None = None,
    validations: list[dict[str, Any]] | None = None,
    dispositions: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    entries = [from_model_attempt(attempt) for attempt in attempts]
    if plan and plan.get("created_ts"):
        entries.append(from_run_plan(plan))
    entries.extend(from_round_decision(decision) for decision in decisions)
    entries.extend(from_candidate_event(event) for event in candidate_events or [])
    entries.extend(from_validation(validation) for validation in validations or [])
    entries.extend(from_disposition(disposition) for disposition in dispositions or [])
    return sorted(entries, key=lambda item: (str(item["timestamp"]), str(item["action"])))
