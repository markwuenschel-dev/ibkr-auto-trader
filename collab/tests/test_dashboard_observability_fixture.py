from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard_core as dc  # noqa: E402
import serve_dashboard_observability_fixture as fixture  # noqa: E402


def test_representative_fixture_retains_two_of_three_four_model_truth(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    home = tmp_path / "home"
    lease = fixture.create_fixture(collab, home)
    try:
        live = dc.snapshot(collab, home=home)
    finally:
        lease.release()

    assert live["status"]["phase"] == "done"
    assert live["status"]["active_seat"] is None
    assert live["health"]["run_archive_persistence"]["status"] == "healthy"
    assert live["run_plan"]["rounds"] == {
        "boundary": "candidate_assessment_cycle",
        "maximum": 3,
        "minimum": 0,
        "planned": 3,
        "required": 0,
    }
    assert live["latest_decision"]["completed_round"] == 2
    assert live["latest_decision"]["maximum_rounds"] == 3
    assert live["latest_decision"]["decision"] == "stop"
    assert live["latest_decision"]["reason"] == "accepted_result_met_completion_criteria"
    assert len(live["model_activity"]) == 4
    assert {attempt["requested_model"] for attempt in live["model_activity"]} == {
        "gpt-5.6-luna",
        "grok-4.5",
        "gemini-3.5-flash",
        "haiku-4.5",
    }
    assert {
        item["model"]: item["terminal_disposition"] for item in live["execution_roster"]
    } == {
        "gpt-5.6-luna": "completed_and_evaluated",
        "grok-4.5": "completed_and_evaluated",
        "gemini-3.5-flash": "completed_and_evaluated",
        "haiku-4.5": "completed_and_evaluated",
    }
    for attempt in live["model_activity"]:
        assert "streaming" in attempt["states"]
        assert attempt["gateway_route"] in {"responses", "chat/completions"}
        assert attempt["telemetry_state"] == "telemetry_verified"

    replay = dc.run_detail(collab, fixture.RUN_UID)
    assert replay["latest_decision"] == live["latest_decision"]
    assert replay["operator_run_summary"]["truth_status"] == "incomplete"
    assert "fresh xAI provider success" in replay["operator_run_summary"]["missing_evidence"]
    assert replay["manifest"]["state"] == "sealed"
