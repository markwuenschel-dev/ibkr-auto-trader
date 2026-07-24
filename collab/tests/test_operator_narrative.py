"""Evidence-aware operator timeline entries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import operator_narrative as on  # noqa: E402


def test_round_decision_entry_answers_operator_questions() -> None:
    entry = on.from_round_decision(
        {
            "decision_id": "decision-1",
            "run_uid": "run-1",
            "completed_round": 2,
            "maximum_rounds": 3,
            "decision": "stop",
            "reason": "required_dependency_unavailable",
            "candidate_id": "cand:abc",
            "timestamp": "2026-07-22T12:00:00Z",
            "supporting_evidence": ["assessment:cand:abc"],
        }
    )
    assert entry["actor"] == "autopilot.round_decision"
    assert entry["action"] == "stopped after completed round 2 of maximum 3"
    assert entry["reason"] == "required dependency unavailable"
    assert entry["consequence"] == "another round was not started"
    assert entry["operator_action"] == "restore the dependency or choose an explicit retry"
    assert entry["stable_ids"] == {"run_uid": "run-1", "candidate_id": "cand:abc"}
    on.validate(entry)


def test_model_attempt_entry_distinguishes_gateway_failure_from_telemetry() -> None:
    entry = on.from_model_attempt(
        {
            "run_uid": "run-1",
            "attempt_id": "attempt-1",
            "request_id": "request-1",
            "seat": "reviewer",
            "requested_model": "grok-4.5",
            "state": "failed",
            "failure_classification": "http_error",
            "telemetry_state": "telemetry_verified",
            "updated_ts": "2026-07-22T12:00:00Z",
        }
    )
    assert entry["action"] == "model attempt failed"
    assert entry["reason"] == "http error"
    assert "telemetry was verified" in entry["consequence"]
    on.validate(entry)


def test_completed_attempt_with_telemetry_failure_requires_operator_action() -> None:
    entry = on.from_model_attempt(
        {
            "run_uid": "run-1",
            "attempt_id": "attempt-2",
            "request_id": "request-2",
            "seat": "builder",
            "requested_model": "gpt-5.6-luna",
            "state": "completed",
            "telemetry_state": "telemetry_failed",
            "updated_ts": "2026-07-22T12:00:00Z",
        }
    )
    assert entry["operator_action"] == "inspect the telemetry failure and reconcile or retry export"
    assert entry["next_action"] == "reconcile the retained request against Langfuse"


def test_run_plan_entry_explains_the_declared_start_strategy() -> None:
    entry = on.from_run_plan(
        {
            "run_uid": "run-1",
            "created_ts": "2026-07-22T11:59:00Z",
            "objective": "Make the run explainable",
            "plain_language_strategy": "This run may use up to 3 rounds and can stop early.",
            "rounds": {"maximum": 3},
            "execution_roster": [{"role": "builder"}, {"role": "reviewer"}],
        }
    )
    assert entry["action"] == "run started with a declared plan"
    assert entry["reason"] == "This run may use up to 3 rounds and can stop early."
    assert entry["consequence"] == "up to 3 rounds and 2 configured roles became eligible"
    on.validate(entry)


def test_project_includes_candidate_validation_and_disposition_narrative() -> None:
    entries = on.project(
        attempts=[],
        decisions=[],
        candidate_events=[
            {
                "record_type": "candidate_evidence",
                "event_type": "candidate_created",
                "run_uid": "run-1",
                "candidate_id": "candidate-a",
                "handoff_id": "030",
                "timestamp": "2026-07-22T12:00:00Z",
                "producer": {"role": "builder", "model": "gpt-5.6-luna"},
                "files": ["a.py"],
            }
        ],
        validations=[
            {
                "record_type": "validation_evidence",
                "run_uid": "run-1",
                "candidate_id": "candidate-a",
                "validation_id": "pytest:focused",
                "timestamp": "2026-07-22T12:01:00Z",
                "source_kind": "automated_check",
                "status": "failed",
                "producer": "pytest",
                "producer_version": "9.1",
                "artifact_ref": "verification/pytest.json",
                "gaps": ["two integration tests failed"],
            }
        ],
        dispositions=[
            {
                "record_type": "candidate_disposition",
                "run_uid": "run-1",
                "candidate_id": "candidate-a",
                "handoff_id": "030",
                "timestamp": "2026-07-22T12:02:00Z",
                "disposition": "rejected",
                "decision_maker": "done-contract",
                "executive_explanation": "The integration replay checks failed.",
                "impact": "Historical state would be wrong after restart.",
                "remediation": "Repair replay and rerun the named checks.",
                "human_review_triggers": [],
                "evidence_refs": ["validation:pytest:focused"],
            }
        ],
    )
    assert [entry["action"] for entry in entries] == [
        "produced candidate",
        "validation failed",
        "candidate rejected",
    ]
    assert entries[0]["actor"] == "builder (gpt-5.6-luna)"
    assert entries[1]["reason"] == "automated check from pytest 9.1"
    assert entries[1]["operator_action"] == "review the failed evidence and remediation"
    assert entries[2]["consequence"] == "Historical state would be wrong after restart."
    assert entries[2]["next_action"] == "Repair replay and rerun the named checks."
    assert all(entry["confidence"] == "verified_projection" for entry in entries)


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "completed"),
        ("reason", "stopping condition met"),
        ("consequence", "something happened"),
        ("evidence_links", []),
        ("timestamp", ""),
    ],
)
def test_linter_rejects_generic_or_unattributed_entries(field: str, value) -> None:
    entry = {
        "actor": "autopilot",
        "action": "evaluated candidate cand:abc",
        "object": "candidate cand:abc",
        "reason": "all required validations passed",
        "consequence": "candidate became eligible for sign-off",
        "next_action": "evaluate the done contract",
        "operator_action": "none",
        "stable_ids": {"candidate_id": "cand:abc"},
        "evidence_links": ["assessment:cand:abc"],
        "source_type": "round_decision",
        "timestamp": "2026-07-22T12:00:00Z",
        "producer": "operator-narrative-v1",
        "confidence": "verified_projection",
    }
    entry[field] = value
    with pytest.raises(on.OperatorNarrativeError):
        on.validate(entry)
