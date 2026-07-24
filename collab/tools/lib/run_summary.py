"""Evidence-aware, human-facing durable run summary projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import candidate_disposition as cd
import candidate_evidence as ce
import model_observability as mo
import quality_evidence as qe
import requirements_matrix as req
import round_decision as rd
import run_plan as rp
import run_projection as rproj

SCHEMA_VERSION = "1.0"


def retained_persistence_failures(run_dir: str | Path) -> list[str]:
    root = Path(run_dir)
    labels = {
        "model-observability-health.json": "model attempt ledger persistence",
        "model-calls-health.json": "redacted call ledger persistence",
        "run-events-health.json": "structured run-evidence ledger persistence",
    }
    failures: list[str] = []
    for name, label in labels.items():
        try:
            value = json.loads((root / name).read_text("utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            failures.append(f"{label} health record is unreadable")
            continue
        if not isinstance(value, dict):
            failures.append(f"{label} health record is malformed")
            continue
        status = str(value.get("status") or "unknown")
        if status in ("degraded", "unavailable"):
            reason = str(value.get("reason") or "reason not recorded")
            failures.append(f"{label} {status}: {reason}")
    return failures


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value not in (None, "")))


def build(
    projection: dict[str, Any], *, legacy_summary: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Separate proven facts, evaluator judgments, model claims, and missing evidence."""
    legacy = legacy_summary or {}
    plan = projection.get("plan") or {}
    attempts = list(projection.get("attempts") or [])
    roster = list(projection.get("roster") or [])
    candidates = list(projection.get("candidates") or [])
    validations = list(projection.get("validations") or [])
    requirements = list(projection.get("requirements") or [])
    dispositions = list(projection.get("dispositions") or [])
    decision = projection.get("latest_decision") or {}

    models_attempted = _unique([item.get("requested_model") for item in attempts])
    gateway_attempts = [
        item for item in attempts if "gateway_accepted" in (item.get("states") or [])
    ]
    provider_responses = [
        item
        for item in attempts
        if item.get("source") == "gateway_client" and item.get("state") == "completed"
    ]
    telemetry_verified = [
        item for item in attempts if item.get("telemetry_state") == "telemetry_verified"
    ]
    telemetry_failed = [
        item for item in attempts if item.get("telemetry_state") == "telemetry_failed"
    ]
    automated = [item for item in validations if item.get("source_kind") == "automated_check"]
    judgments = [
        {
            "candidate_id": item.get("candidate_id"),
            "disposition": item.get("disposition"),
            "explanation": item.get("executive_explanation"),
            "impact": item.get("impact"),
            "weaknesses": list(item.get("weaknesses") or []),
            "confidence": item.get("confidence"),
            "evidence_refs": list(item.get("evidence_refs") or []),
            "decision_maker": item.get("decision_maker"),
            "primary_reason": item.get("primary_reason"),
            "reason_categories": list(item.get("reason_categories") or []),
            "failed_checks": list(item.get("failed_checks") or []),
            "remediation": item.get("remediation"),
            "resolution": item.get("resolution"),
            "unavailable_evidence": list(item.get("unavailable_evidence") or []),
            "requirements_evaluation": dict(item.get("requirements_evaluation") or {}),
        }
        for item in dispositions
    ]
    model_claims = [
        {
            "validation_id": item.get("validation_id"),
            "candidate_id": item.get("candidate_id"),
            "status": item.get("status"),
            "artifact_ref": item.get("artifact_ref"),
        }
        for item in validations
        if item.get("source_kind") == "model_self_report"
    ]

    missing: list[str] = []
    if not projection.get("plan"):
        missing.append("run plan was not retained")
    for item in roster:
        if item.get("state") in ("selected", "configured"):
            missing.append(f"expected model {item.get('model') or 'unknown'} has no invocation")
        disposition = item.get("terminal_disposition")
        if decision.get("decision") == "stop" and not disposition:
            missing.append(
                f"expected model {item.get('model') or 'unknown'} has no terminal disposition"
            )
        if disposition == "completed_without_gateway_attempt":
            missing.append(
                f"expected model {item.get('model') or 'unknown'} completed without LiteLLM evidence"
            )
        if disposition and (
            "not_evaluated" in str(disposition) or "telemetry_missing" in str(disposition)
        ):
            missing.append(
                f"expected model {item.get('model') or 'unknown'} ended as {disposition}"
            )
    for item in attempts:
        if item.get("state") in ("completed", "failed", "timed_out", "cancelled") and not item.get(
            "telemetry_state"
        ):
            missing.append(f"attempt {item.get('attempt_id')} has no Langfuse outcome")
    evaluated_candidates = {
        item.get("candidate_id")
        for item in (*validations, *dispositions)
        if item.get("candidate_id")
        and (
            item.get("record_type") == "candidate_disposition"
            or item.get("source_kind") != "model_self_report"
        )
    }
    for item in attempts:
        candidate_id = item.get("candidate_id")
        if (
            candidate_id
            and item.get("state") == "completed"
            and candidate_id not in evaluated_candidates
        ):
            missing.append(
                f"attempt {item.get('attempt_id')} produced candidate {candidate_id} "
                "with no evaluation record"
            )
    for item in candidates:
        if item.get("candidate_id") not in evaluated_candidates:
            missing.append(f"candidate {item.get('candidate_id')} has no validation evidence")
    for item in dispositions:
        missing.extend(str(value) for value in item.get("unavailable_evidence") or [])
    health = projection.get("evidence_health") or {}
    if health.get("lifecycle_conflicts"):
        missing.append(f"{health['lifecycle_conflicts']} lifecycle conflict(s) remain")
    missing.extend(str(value) for value in health.get("failures") or [])
    missing.extend(str(value) for value in health.get("gaps") or [])
    missing = _unique(missing)

    human_actions = _unique(
        [
            trigger
            for item in dispositions
            for trigger in item.get("human_review_triggers") or []
        ]
    )
    files_changed = _unique(
        [path for candidate in candidates for path in candidate.get("files") or []]
    )
    requirement_counts: dict[str, int] = {}
    for item in requirements:
        status = str(item.get("effective_status") or item.get("status") or "unknown")
        requirement_counts[status] = requirement_counts.get(status, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "operator_run_summary",
        "run_uid": projection.get("run_uid"),
        "objective": plan.get("objective") or "not recorded",
        "truth_status": "complete" if not missing else "incomplete",
        "outcome": {
            "decision": decision.get("decision") or "not recorded",
            "stop_reason": decision.get("reason") or legacy.get("terminal_reason") or "not recorded",
            "completed_rounds": decision.get("completed_round"),
            "maximum_rounds": decision.get("maximum_rounds")
            if decision.get("maximum_rounds") is not None
            else (plan.get("rounds") or {}).get("maximum"),
            "legacy_actor_turns": legacy.get("rounds_total"),
        },
        "proven_facts": {
            "models_expected": _unique([item.get("model") for item in roster]),
            "models_attempted": models_attempted,
            "calls_reaching_litellm": len(gateway_attempts),
            "provider_responses": len(provider_responses),
            "calls_reaching_langfuse": len(telemetry_verified),
            "explicit_telemetry_failures": len(telemetry_failed),
            "candidates_produced": [item.get("candidate_id") for item in candidates],
            "candidate_artifacts": [
                {
                    "candidate_id": item.get("candidate_id"),
                    "parent_candidate_id": item.get("parent_candidate_id"),
                    "files": list(item.get("files") or []),
                    "patch_digest": item.get("patch_digest"),
                    "final_artifact_ref": item.get("final_artifact_ref"),
                    "final_commit": item.get("final_commit"),
                    "revision_evidence_refs": list(item.get("revision_evidence_refs") or []),
                }
                for item in candidates
            ],
            "automated_validations": [
                {
                    "validation_id": item.get("validation_id"),
                    "candidate_id": item.get("candidate_id"),
                    "status": item.get("status"),
                    "artifact_ref": item.get("artifact_ref"),
                    "producer": item.get("producer"),
                    "producer_version": item.get("producer_version"),
                    "dimensions": dict(item.get("dimensions") or {}),
                    "baseline_delta": dict(item.get("baseline_delta") or {}),
                    "uncertainty": item.get("uncertainty"),
                    "gaps": list(item.get("gaps") or []),
                    "test_quality": dict(item.get("test_quality") or {}),
                    "is_acceptance_oracle": bool(item.get("is_acceptance_oracle")),
                }
                for item in automated
            ],
            "requirement_statuses": requirement_counts,
            "files_changed": files_changed,
        },
        "evaluator_judgments": judgments,
        "model_claims": model_claims,
        "missing_evidence": missing,
        "known_risks": _unique(
            [risk for item in dispositions for risk in item.get("weaknesses") or []]
        ),
        "human_actions": human_actions,
        "evidence_links": {
            "attempts": [f"attempt:{item.get('attempt_id')}" for item in attempts],
            "candidates": [f"candidate:{item.get('candidate_id')}" for item in candidates],
            "validations": [f"validation:{item.get('validation_id')}" for item in validations],
        },
    }


def build_from_archive(run_dir: str | Path, *, run_uid: str) -> dict[str, Any]:
    """Build only from the retained archive immediately before it is sealed."""
    root = Path(run_dir)
    try:
        legacy = json.loads((root / "run.json").read_text("utf-8"))
    except (OSError, ValueError):
        legacy = {}
    plan = rp.read_plan(root.parent.parent.parent, run_uid=run_uid)
    try:
        archived_plan = json.loads((root / "run-plan.json").read_text("utf-8"))
        if isinstance(archived_plan, dict) and archived_plan.get("run_uid") == run_uid:
            plan = archived_plan
    except (OSError, ValueError):
        pass
    model_events = mo.read_events(root / "model-events.jsonl")
    decisions = rd.read_decision_file(root / "run-events.jsonl", run_uid=run_uid)
    candidate_events = ce.read_event_file(root / "run-events.jsonl", run_uid=run_uid)
    validations = qe.read_validation_file(root / "run-events.jsonl", run_uid=run_uid)
    requirements = req.project(
        req.read_requirement_file(root / "run-events.jsonl", run_uid=run_uid)
    )
    dispositions = cd.read_disposition_file(root / "run-events.jsonl", run_uid=run_uid)
    projection = rproj.project(
        run_uid=run_uid,
        plan=plan,
        model_events=model_events,
        decisions=decisions,
        candidate_events=candidate_events,
        validations=validations,
        requirements=requirements,
        dispositions=dispositions,
    )
    projection["evidence_health"]["failures"].extend(retained_persistence_failures(root))
    return build(projection, legacy_summary=legacy if isinstance(legacy, dict) else {})
