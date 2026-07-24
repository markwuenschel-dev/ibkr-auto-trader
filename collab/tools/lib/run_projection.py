"""Pure shared projection for live and archived run evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import candidate_evidence
import model_observability as mo
import operator_narrative
import run_plan


def project(
    *,
    run_uid: str,
    plan: dict[str, Any] | None,
    model_events: Iterable[mo.ModelAttemptEvent],
    decisions: list[dict[str, Any]],
    candidate_events: list[dict[str, Any]] | None = None,
    validations: list[dict[str, Any]] | None = None,
    requirements: list[dict[str, Any]] | None = None,
    dispositions: list[dict[str, Any]] | None = None,
    manifest_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce the same retained facts identically regardless of live/archive storage location."""
    attempts = mo.reduce_attempts(event for event in model_events if event.run_uid == run_uid)
    validation_records = list(validations or [])
    disposition_records = list(dispositions or [])
    evaluated_candidate_ids = {
        str(item.get("candidate_id"))
        for item in (*validation_records, *disposition_records)
        if item.get("candidate_id")
        and (
            item.get("record_type") == "candidate_disposition"
            or item.get("source_kind") != "model_self_report"
        )
    }
    roster = run_plan.project_roster(
        plan,
        attempts,
        evaluated_candidate_ids=evaluated_candidate_ids,
        terminal_decision=decisions[-1] if decisions else None,
    )
    candidates = candidate_evidence.project(candidate_events or [])
    conflicts = sum(len(attempt.get("conflicts") or []) for attempt in attempts)
    if manifest_verification is None:
        archive_integrity = "unknown"
        manifest_state = "not_applicable"
        manifest_failures: list[str] = []
        gaps: list[str] = []
    else:
        archive_integrity = "healthy" if manifest_verification.get("valid") else "unavailable"
        manifest_state = str(manifest_verification.get("state") or "unknown")
        manifest_failures = [str(item) for item in manifest_verification.get("failures") or []]
        gaps = [str(item) for item in manifest_verification.get("gaps") or []]
    return {
        "run_uid": run_uid,
        "plan": plan,
        "attempts": attempts,
        "roster": roster,
        "decisions": decisions,
        "candidates": candidates,
        "validations": validation_records,
        "requirements": list(requirements or []),
        "dispositions": disposition_records,
        "latest_decision": decisions[-1] if decisions else None,
        "telemetry": [
            {
                "attempt_id": attempt.get("attempt_id"),
                "request_id": attempt.get("request_id"),
                "state": attempt.get("telemetry_state"),
                "result": attempt.get("telemetry_result"),
                "observation_id": attempt.get("observation_id"),
                "trace_id": attempt.get("trace_id"),
            }
            for attempt in attempts
        ],
        "narrative": operator_narrative.project(
            attempts=attempts,
            decisions=decisions,
            candidate_events=list(candidate_events or []),
            validations=validation_records,
            dispositions=disposition_records,
            plan=plan,
        ),
        "evidence_health": {
            "archive_integrity": archive_integrity,
            "manifest_state": manifest_state,
            "failures": manifest_failures,
            "gaps": gaps,
            "lifecycle_conflicts": conflicts,
        },
    }
