"""Immutable per-run execution plan and truthful participant-roster projection."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collab_common as cc

SCHEMA_VERSION = "1.0"


class RunPlanError(cc.CollabError):
    """The declared run plan is invalid or conflicts with durable evidence."""


def _path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-plan.json"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _roster(seats: dict[str, Any]) -> list[dict[str, Any]]:
    default_tasks = {
        "builder": "produce or revise the candidate implementation",
        "reviewer": "evaluate the candidate and decide whether it is eligible",
        "breaker": "probe the candidate for concrete failures",
        "verifier": "confirm or refute retained findings and verification evidence",
    }
    roster: list[dict[str, Any]] = []
    for role in sorted(seats):
        raw = seats[role]
        config = raw if isinstance(raw, dict) else {}
        backend = str(config.get("backend") or "")
        eligible = backend == "cli" and bool(config.get("cmd"))
        roster.append(
            {
                "role": str(role),
                "model": config.get("model"),
                "backend": backend or None,
                "configured": True,
                "eligible": eligible,
                "selected": eligible,
                "disabled_reason": None if eligible else "non_cli_backend",
                "selection_reason": (
                    f"configured eligible {role} role" if eligible else "not eligible for invocation"
                ),
                "assigned_task": config.get("assignment")
                or default_tasks.get(str(role), f"perform the configured {role} role"),
            }
        )
    return roster


def declare(
    collab: str | Path,
    *,
    run_uid: str,
    seats: dict[str, Any],
    limits: Any,
    objective: str,
    created_ts: str | None = None,
) -> dict[str, Any]:
    """Atomically declare the run before dispatch; same-run mutation fails closed."""
    if not run_uid or not objective.strip():
        raise RunPlanError("run_uid and objective are required")
    budget = asdict(limits)
    work_max = int(budget["max_work_attempts"])
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "run_plan",
        "run_uid": run_uid,
        "created_ts": created_ts or _timestamp(),
        "objective": objective.strip(),
        "rounds": {
            "boundary": "candidate_assessment_cycle",
            "minimum": 0,
            "planned": work_max,
            "required": 0,
            "maximum": work_max,
        },
        "budgets": {
            "work_attempts": work_max,
            "verification_passes": int(budget["max_verification_passes"]),
            "total_model_calls": int(budget["max_total_model_calls"]),
            "wall_clock_seconds": float(budget["max_wall_clock_seconds"]),
            "findings_per_lane": int(budget["max_findings_per_lane"]),
            "review_decisions_per_candidate": int(
                budget["max_review_decisions_per_candidate"]
            ),
        },
        "policies": {
            "early_stop": "only_on_terminal_evidence_or_human_control",
            "force_continue": "human_authorized_new_epoch_only",
            "rotation": "role_bound_seat",
            "retry": "new_attempt_identity_preserving_parent_lineage",
            "concurrency": "verification_plan_defined",
            "revision": "blocking_findings_return_to_builder",
            "repeat": "byte_identical_candidate_stops_as_no_progress",
            "evaluator_version": "candidate-assessment-v1",
        },
        "guarantees": {"every_model_attempted": False},
        "plain_language_strategy": (
            f"This run may use up to {work_max} candidate-assessment rounds. It can stop early only "
            "on terminal evidence or explicit human control; not every configured model is guaranteed "
            "an attempt."
        ),
        "execution_roster": _roster(seats),
    }
    path = _path(collab)
    existing = read_plan(collab)
    if existing and existing.get("run_uid") == run_uid:
        if existing != plan:
            raise RunPlanError(f"run plan for {run_uid} conflicts with its immutable declaration")
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(path, json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return plan


def read_plan(collab: str | Path, *, run_uid: str | None = None) -> dict[str, Any] | None:
    try:
        value = json.loads(_path(collab).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    if run_uid is not None and value.get("run_uid") != run_uid:
        return None
    return value


def project_roster(
    plan: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    *,
    evaluated_candidate_ids: set[str] | None = None,
    terminal_decision: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project distinct orchestration/transport/telemetry milestones for each planned role."""
    entries = (plan or {}).get("execution_roster") or []
    evaluated_candidates = evaluated_candidate_ids or set()
    projected: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        model = raw.get("model")
        related = [
            item
            for item in attempts
            if item.get("seat") == role
            and (model is None or item.get("requested_model") in (None, model))
        ]
        states = {state for item in related for state in (item.get("states") or [])}
        sources = {item.get("source") for item in related}
        queued = bool(states & {"queued", "starting", "connecting"})
        invoked = bool(related)
        gateway_reached = "gateway_accepted" in states
        provider_returned = any(
            item.get("source") == "gateway_client" and item.get("state") == "completed"
            for item in related
        )
        telemetry_reconciled = any(
            item.get("telemetry_state") in ("telemetry_verified", "telemetry_failed")
            for item in related
        )
        telemetry_states = {
            item.get("telemetry_state")
            for item in related
            if item.get("telemetry_state") in ("telemetry_verified", "telemetry_failed")
        }
        telemetry_outcome = (
            "mixed"
            if len(telemetry_states) > 1
            else "verified"
            if "telemetry_verified" in telemetry_states
            else "failed"
            if "telemetry_failed" in telemetry_states
            else "missing"
            if provider_returned
            else "not_applicable"
        )
        provider_attempt_count = sum(
            1 for item in related if item.get("source") == "gateway_client"
        )
        orchestration_execution_count = sum(
            1
            for item in related
            if item.get("source") in ("orchestrator", "parent_execution")
        )
        evaluated = any(item.get("candidate_id") in evaluated_candidates for item in related)
        failed_before_invocation = bool(
            related
            and not gateway_reached
            and any(item.get("state") in ("failed", "timed_out", "cancelled") for item in related)
            and bool(sources & {"orchestrator", "parent_execution"})
        )
        stopped = bool((terminal_decision or {}).get("decision") == "stop")
        skipped = bool(
            raw.get("eligible")
            and (
                raw.get("selected") is False
                or (stopped and raw.get("selected") and not related)
            )
        )
        disabled = not bool(raw.get("eligible"))
        if telemetry_reconciled:
            state = "telemetry_reconciled"
        elif provider_returned:
            state = "provider_returned"
        elif gateway_reached:
            state = "gateway_reached"
        elif failed_before_invocation:
            state = "failed_before_invocation"
        elif invoked:
            state = "invoked"
        elif skipped:
            state = "skipped"
        elif disabled:
            state = "disabled"
        elif raw.get("selected"):
            state = "selected"
        else:
            state = "configured"
        terminal_disposition: str | None = None
        if disabled:
            terminal_disposition = "disabled_by_configuration"
        elif skipped:
            terminal_disposition = "skipped_after_terminal_decision" if stopped else "skipped"
        elif failed_before_invocation:
            terminal_disposition = "failed_before_invocation"
        else:
            provider_failures = [
                str(item.get("state"))
                for item in related
                if item.get("source") == "gateway_client"
                and item.get("state") in ("failed", "timed_out", "cancelled")
            ]
            parent_completed = any(
                item.get("source") in ("orchestrator", "parent_execution")
                and item.get("state") == "completed"
                for item in related
            )
            if provider_returned:
                base = "completed_and_evaluated" if evaluated else "completed_not_evaluated"
                terminal_disposition = (
                    base
                    if telemetry_outcome == "verified"
                    else f"{base}_telemetry_{telemetry_outcome}"
                )
            elif provider_failures:
                terminal_disposition = f"provider_{provider_failures[-1]}"
            elif parent_completed and provider_attempt_count == 0:
                terminal_disposition = "completed_without_gateway_attempt"
        projected.append(
            {
                **raw,
                "queued": queued,
                "invoked": invoked,
                "gateway_reached": gateway_reached,
                "provider_returned": provider_returned,
                "telemetry_reconciled": telemetry_reconciled,
                "evaluated": evaluated,
                "skipped": skipped,
                "disabled": disabled,
                "failed_before_invocation": failed_before_invocation,
                "state": state,
                "attempt_count": len(related),
                "provider_attempt_count": provider_attempt_count,
                "orchestration_execution_count": orchestration_execution_count,
                "telemetry_outcome": telemetry_outcome,
                "terminal_disposition": terminal_disposition,
                "terminal_reason": (
                    (terminal_decision or {}).get("reason")
                    if terminal_disposition == "skipped_after_terminal_decision"
                    else None
                ),
            }
        )
    return projected
