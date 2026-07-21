"""Canonical operational lifecycle contract and append-only replay tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(LIB))

import autopilot as ap  # noqa: E402
import escalation  # noqa: E402
import handoff_core as hc  # noqa: E402
import operational_state as ops  # noqa: E402
import operator_requests as opreq  # noqa: E402
import transitions  # noqa: E402

ALL_STATES = {
    "queued",
    "claimed",
    "running",
    "awaiting",
    "paused",
    "capped",
    "blocked",
    "parked",
    "escalated",
    "retrying",
    "failed",
    "cancelled",
    "superseded",
    "completed",
}


def event(
    event_id: str,
    new_state: str,
    *,
    previous_state: str | None = None,
    event_ts: str = "2026-07-21T20:00:00Z",
    ingested_ts: str = "2026-07-21T20:00:01Z",
    source: str = "test",
    conditions: tuple[str, ...] = (),
) -> ops.OperationalEvent:
    return ops.OperationalEvent(
        event_id=event_id,
        entity_id="035",
        previous_state=(ops.OperationalState(previous_state) if previous_state else None),
        new_state=ops.OperationalState(new_state),
        reason=f"because-{new_state}",
        source=source,
        actor="builder",
        run_id="run-1",
        event_ts=event_ts,
        ingested_ts=ingested_ts,
        correlation_id="corr-1",
        trace_id="trace-1",
        conditions=conditions,
    )


def test_contract_contains_every_operator_state() -> None:
    assert {state.value for state in ops.OperationalState} == ALL_STATES
    assert set(ops.STATE_PRECEDENCE) == set(ops.OperationalState)


def test_event_round_trip_carries_required_audit_fields() -> None:
    original = ops.OperationalEvent(
        event_id="evt-1",
        entity_id="035",
        previous_state=ops.OperationalState.CAPPED,
        new_state=ops.OperationalState.ESCALATED,
        reason="verification_incomplete",
        source="escalation_store",
        actor="autopilot",
        run_id="run-1",
        event_ts="2026-07-21T20:00:00Z",
        ingested_ts="2026-07-21T20:00:01Z",
        correlation_id="req-1",
        trace_id="trace-1",
        conditions=("parked",),
        escalation_severity="warning",
        escalation_reason="verification_incomplete",
        escalation_ts="2026-07-21T20:00:00Z",
        required_action="retry_or_adopt",
    )
    doc = original.to_dict()
    assert doc["schema_version"] == "1.0"
    assert ops.OperationalEvent.from_dict(doc) == original


def test_replay_is_deterministic_for_duplicate_and_out_of_order_delivery() -> None:
    queued = event("evt-1", "queued", event_ts="2026-07-21T20:00:00Z")
    claimed = event(
        "evt-2",
        "claimed",
        previous_state="queued",
        event_ts="2026-07-21T20:01:00Z",
    )
    running = event(
        "evt-3",
        "running",
        previous_state="claimed",
        event_ts="2026-07-21T20:02:00Z",
    )
    canonical = ops.reduce_history([queued, claimed, running])
    replayed = ops.reduce_history([running, queued, claimed, running, claimed])
    assert replayed.state is ops.OperationalState.RUNNING
    assert replayed.events == canonical.events
    assert replayed.duplicate_count == 2
    assert replayed.conflicts == ()


def test_same_event_id_with_different_payload_is_a_conflict() -> None:
    first = event("evt-1", "queued")
    changed = event("evt-1", "failed")
    reduced = ops.reduce_history([first, changed])
    assert reduced.state is ops.OperationalState.QUEUED
    assert reduced.duplicate_count == 0
    assert reduced.conflicts[0].kind == "event_id_collision"


def test_invalid_transition_is_retained_and_surfaced() -> None:
    queued = event("evt-1", "queued")
    completed = event(
        "evt-2",
        "completed",
        previous_state="running",
        event_ts="2026-07-21T20:01:00Z",
    )
    reduced = ops.reduce_history([completed, queued])
    assert reduced.state is ops.OperationalState.COMPLETED
    assert reduced.conflicts[0].kind == "previous_state_mismatch"
    assert reduced.conflicts[0].event_id == "evt-2"


def test_transition_matrix_surfaces_a_semantically_invalid_jump() -> None:
    queued = event("evt-1", "queued")
    failed = event(
        "evt-2",
        "failed",
        previous_state="queued",
        event_ts="2026-07-21T20:01:00Z",
    )
    reduced = ops.reduce_history([queued, failed])
    assert reduced.state is ops.OperationalState.FAILED
    assert [(conflict.kind, conflict.event_id) for conflict in reduced.conflicts] == [
        ("invalid_transition", "evt-2")
    ]


def test_reconciliation_can_record_an_explicit_correction_across_states() -> None:
    queued = event("evt-1", "queued")
    corrected = event(
        "evt-2",
        "failed",
        previous_state="queued",
        event_ts="2026-07-21T20:01:00Z",
        source="reconciliation",
    )
    reduced = ops.reduce_history([queued, corrected])
    assert reduced.state is ops.OperationalState.FAILED
    assert reduced.conflicts == ()


def test_late_correction_is_a_subsequent_event_not_a_rewrite() -> None:
    queued = event("evt-1", "queued")
    completed = event(
        "evt-2",
        "completed",
        previous_state="queued",
        event_ts="2026-07-21T20:01:00Z",
    )
    correction = event(
        "evt-3",
        "cancelled",
        previous_state="completed",
        event_ts="2026-07-21T20:02:00Z",
        source="reconciliation",
    )
    reduced = ops.reduce_history([queued, completed, correction])
    assert reduced.state is ops.OperationalState.CANCELLED
    assert [item.event_id for item in reduced.events] == ["evt-1", "evt-2", "evt-3"]


def test_append_is_idempotent_and_pages_by_stable_sequence(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    queued = event("evt-1", "queued")
    claimed = event("evt-2", "claimed", previous_state="queued")

    first = ops.append_event(collab, queued)
    duplicate = ops.append_event(collab, queued)
    second = ops.append_event(collab, claimed)

    assert first.appended is True and first.event.sequence == 1
    assert duplicate.appended is False and duplicate.event.sequence == 1
    assert second.appended is True and second.event.sequence == 2

    page1 = ops.read_history(collab, "035", limit=1)
    page2 = ops.read_history(collab, "035", after=page1.next_cursor, limit=10)
    assert [item.event_id for item in page1.events] == ["evt-1"]
    assert [item.event_id for item in page2.events] == ["evt-2"]
    assert page2.next_cursor == 2


def test_history_reader_counts_malformed_and_incompatible_records(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    ops.append_event(collab, event("evt-1", "queued"))
    path = collab / "autopilot" / "lifecycle" / "035.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
        stream.write(json.dumps({"schema_version": "99.0", "event_id": "future"}) + "\n")

    page = ops.read_history(collab, "035")
    assert page.rejected_count == 2
    assert page.schema_incompatible is True


@pytest.mark.parametrize("bad_id", ["", "../035", "35.json", "abc", "0" * 10])
def test_history_entity_id_is_a_path_safe_handoff_id(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ops.OperationalStateError):
        ops.read_history(tmp_path, bad_id)


def test_handoff_state_machine_emits_canonical_history(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    created = hc.create(collab, to="reviewer", from_="builder", title="state contract", body="x")
    hc.claim(collab, created["id"])
    hc.done(
        collab,
        created["id"],
        kind=transitions.KIND_HUMAN,
        actor="operator",
        reason="accepted explicitly",
    )
    hc.archive(collab, created["id"])

    page = ops.read_history(collab, created["id"])
    assert [item.new_state.value for item in page.events] == [
        "queued",
        "claimed",
        "completed",
        "completed",
    ]
    assert page.events[-1].conditions == ("archived",)
    assert ops.reduce_history(page.events).conflicts == ()


def test_escalation_store_emits_parked_escalated_history_and_metadata(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    created = hc.create(collab, to="reviewer", from_="builder", title="state contract", body="x")
    hc.claim(collab, created["id"])

    escalation.write(
        collab,
        created["id"],
        [],
        attempts=2,
        run_uid="run-7",
        reason="verification_incomplete",
    )

    rec = escalation.read(collab, created["id"])
    assert rec is not None
    assert rec["reason"] == "verification_incomplete"
    assert rec["severity"] == "warning"
    assert rec["timestamp"].endswith("Z")
    reduced = ops.reduce_history(ops.read_history(collab, created["id"]).events)
    assert reduced.state is ops.OperationalState.ESCALATED
    assert reduced.events[-1].conditions == ("parked",)
    assert reduced.events[-1].required_action == "retry_or_adopt"


def test_operator_retry_is_a_canonical_retrying_event(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    created = hc.create(collab, to="reviewer", from_="builder", title="state contract", body="x")
    hc.claim(collab, created["id"])
    opreq.write(
        collab,
        created["id"],
        opreq.RETRY,
        by="operator",
    )
    reduced = ops.reduce_history(ops.read_history(collab, created["id"]).events)
    assert reduced.state is ops.OperationalState.RETRYING
    assert reduced.events[-1].required_action == "start_driver"


def test_autopilot_status_records_item_bound_phase_changes_not_heartbeats(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    created = hc.create(collab, to="reviewer", from_="builder", title="state contract", body="x")
    hc.claim(collab, created["id"])

    ap._write_status(
        collab,
        run_uid="run-7",
        current_hid=created["id"],
        phase="thinking",
        stage="verify",
        active_seat="verify",
    )
    ap._write_status(collab)  # liveness heartbeat only
    ap._write_status(collab, stage="assess", active_seat="lanes")
    ap._write_status(collab, phase="paused", current_hid=None, active_seat=None)

    events = ops.read_history(collab, created["id"]).events
    assert [item.new_state.value for item in events] == [
        "queued",
        "claimed",
        "running",
        "running",
        "paused",
    ]
    assert [item.reason for item in events[-3:]] == [
        "driver_verify",
        "driver_assess",
        "driver_paused",
    ]
    assert events[-1].run_id == "run-7"
