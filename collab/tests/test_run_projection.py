"""Live and archived evidence use one pure run projection."""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import model_observability as mo  # noqa: E402
import run_projection as rp  # noqa: E402


def _event(event_id: str, state: str) -> mo.ModelAttemptEvent:
    return mo.ModelAttemptEvent(
        event_id=event_id,
        attempt_id="attempt-1",
        request_id="request-1",
        run_uid="run-1",
        seat="builder",
        requested_model="gpt-5.6-luna",
        state=state,
        event_ts="2026-07-22T12:00:00Z",
        attempt_number=1,
        source="gateway_client",
    )


def test_live_and_replay_inputs_reduce_to_identical_projection() -> None:
    plan = {
        "run_uid": "run-1",
        "execution_roster": [
            {
                "role": "builder",
                "model": "gpt-5.6-luna",
                "configured": True,
                "eligible": True,
                "selected": True,
                "disabled_reason": None,
            }
        ],
    }
    decision = {
        "decision_id": "decision-1",
        "run_uid": "run-1",
        "decision": "stop",
        "completed_round": 1,
        "maximum_rounds": 3,
        "reason": "accepted_result_met_completion_criteria",
        "candidate_id": "cand:abc",
        "timestamp": "2026-07-22T12:00:01Z",
        "supporting_evidence": ["candidate:cand:abc"],
    }
    candidate_event = {
        "schema_version": "1.0",
        "record_type": "candidate_evidence",
        "event_type": "candidate_created",
        "event_id": "candidate-event-1",
        "run_uid": "run-1",
        "candidate_id": "cand:abc",
        "handoff_id": "030",
        "timestamp": "2026-07-22T12:00:00Z",
        "producer": {"role": "builder", "model": "gpt-5.6-luna"},
    }
    events = [_event("connect", "connecting"), _event("done", "completed")]

    live = rp.project(
        run_uid="run-1",
        plan=plan,
        model_events=events,
        decisions=[decision],
        candidate_events=[candidate_event],
    )
    replay = rp.project(
        run_uid="run-1",
        plan=plan,
        model_events=list(events),
        decisions=[decision],
        candidate_events=[candidate_event],
    )

    assert live == replay
    assert live["attempts"][0]["states"] == ["connecting", "completed"]
    assert live["roster"][0]["state"] == "provider_returned"
    assert live["latest_decision"] == decision
    assert live["candidates"][0]["candidate_id"] == "cand:abc"


def test_projection_keeps_all_attempts_beyond_legacy_200_event_tail() -> None:
    events = [
        mo.ModelAttemptEvent(
            event_id=f"event-{index}",
            attempt_id=f"attempt-{index}",
            request_id=f"request-{index}",
            run_uid="run-1",
            seat="builder",
            requested_model="gpt-5.6-luna",
            state="completed",
            event_ts=f"2026-07-22T12:{index // 60:02d}:{index % 60:02d}Z",
            attempt_number=1,
            source="gateway_client",
        )
        for index in range(250)
    ]
    projection = rp.project(run_uid="run-1", plan=None, model_events=events, decisions=[])
    assert len(projection["attempts"]) == 250


def test_projection_reports_conflicts_and_manifest_failures_without_guessing() -> None:
    events = [_event("done", "completed"), _event("late", "generating")]
    projection = rp.project(
        run_uid="run-1",
        plan=None,
        model_events=events,
        decisions=[],
        manifest_verification={
            "valid": False,
            "state": "sealed",
            "failures": ["hash_mismatch:status.json"],
            "gaps": [],
        },
    )
    assert projection["evidence_health"]["archive_integrity"] == "unavailable"
    assert projection["evidence_health"]["lifecycle_conflicts"] == 1
