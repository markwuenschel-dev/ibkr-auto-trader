"""Immutable candidate ownership, lineage, and assessment evidence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import candidate_evidence as ce  # noqa: E402


def test_created_candidate_records_ownership_lineage_and_source_without_model_io(tmp_path: Path) -> None:
    event = ce.record_created(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:two",
        handoff_id="030",
        timestamp="2026-07-22T12:00:00Z",
        producer={"role": "builder", "model": "gpt-5.6-luna", "attempt_id": "attempt-2"},
        parent_candidate_id="cand:one",
        incorporated_candidate_ids=("cand:base",),
        task_version="handoff:030@contract-v2",
        base_commit="abc123",
        worktree_state_hash="sha256:state",
        files=("src/a.py", "tests/test_a.py"),
        patch_digest="sha256:patch",
        tools=("builder-cli",),
        revision_evidence_refs=("finding:f1", "validation:v1"),
        final_artifact_ref="source-manifest:sha256:state",
        final_commit="def456",
    )
    assert event["event_type"] == "candidate_created"
    assert event["producer"]["role"] == "builder"
    assert event["parent_candidate_id"] == "cand:one"
    assert event["incorporated_candidate_ids"] == ["cand:base"]
    assert event["files"] == ["src/a.py", "tests/test_a.py"]
    assert event["revision_evidence_refs"] == ["finding:f1", "validation:v1"]
    assert event["final_artifact_ref"] == "source-manifest:sha256:state"
    assert event["final_commit"] == "def456"
    persisted = (tmp_path / "autopilot" / "run-events.jsonl").read_text("utf-8")
    assert "prompt" not in persisted.lower() and "output" not in persisted.lower()
    assert ce.read_events(tmp_path, run_uid="run-1") == [event]


def test_assessment_event_projects_current_disposition_and_revision_chain(tmp_path: Path) -> None:
    ce.record_created(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:one",
        handoff_id="030",
        timestamp="2026-07-22T12:00:00Z",
        producer={"role": "builder", "model": "gpt", "attempt_id": "attempt-1"},
    )
    ce.record_assessed(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:one",
        handoff_id="030",
        timestamp="2026-07-22T12:01:00Z",
        outcome="repair_required",
        evaluator="candidate-assessment-v1",
        feedback_refs=("finding:f1",),
    )
    projected = ce.project(ce.read_events(tmp_path, run_uid="run-1"))
    assert len(projected) == 1
    assert projected[0]["candidate_id"] == "cand:one"
    assert projected[0]["current_disposition"] == "repair_required"
    assert projected[0]["evaluator_feedback"] == ["finding:f1"]
    assert projected[0]["revision_evidence_refs"] == []
    assert projected[0]["final_artifact_ref"] is None
    assert [item["event_type"] for item in projected[0]["revisions"]] == [
        "candidate_created",
        "candidate_assessed",
    ]


def test_duplicate_event_is_idempotent_but_event_id_collision_fails_closed(tmp_path: Path) -> None:
    event = ce.record_created(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:one",
        handoff_id="030",
        timestamp="2026-07-22T12:00:00Z",
        producer={"role": "builder", "model": "gpt", "attempt_id": "attempt-1"},
    )
    ce.append_event(tmp_path, event)
    assert len(ce.read_events(tmp_path)) == 1
    with pytest.raises(ce.CandidateEvidenceError, match="collision"):
        ce.append_event(tmp_path, {**event, "handoff_id": "999"})
