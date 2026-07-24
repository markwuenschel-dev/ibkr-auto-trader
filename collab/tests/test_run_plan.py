"""Immutable execution-plan and roster contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402
import run_budget as rb  # noqa: E402
import run_manifest as rm  # noqa: E402
import run_plan as rp  # noqa: E402


def _seats() -> dict:
    return {
        "reviewer": {"backend": "cli", "model": "grok-4.5", "cmd": ["review"]},
        "builder": {"backend": "cli", "model": "gpt-5.6-luna", "cmd": ["build"]},
        "human": {"backend": "web", "model": None},
    }


def test_declare_writes_truthful_plan_and_expected_roster(tmp_path: Path) -> None:
    plan = rp.declare(
        tmp_path,
        run_uid="20260722T120000Z-77",
        seats=_seats(),
        limits=rb.Limits.balanced(),
        objective="Assess queued handoffs",
        created_ts="2026-07-22T12:00:00Z",
    )

    assert plan["schema_version"] == "1.0"
    assert plan["run_uid"] == "20260722T120000Z-77"
    assert plan["objective"] == "Assess queued handoffs"
    assert plan["rounds"] == {
        "boundary": "candidate_assessment_cycle",
        "minimum": 0,
        "planned": 3,
        "required": 0,
        "maximum": 3,
    }
    assert plan["budgets"]["work_attempts"] == 3
    assert plan["guarantees"]["every_model_attempted"] is False
    assert plan["plain_language_strategy"] == (
        "This run may use up to 3 candidate-assessment rounds. It can stop early only on "
        "terminal evidence or explicit human control; not every configured model is guaranteed an attempt."
    )
    assert [item["role"] for item in plan["execution_roster"]] == [
        "builder",
        "human",
        "reviewer",
    ]
    human = plan["execution_roster"][1]
    assert human["eligible"] is False and human["disabled_reason"] == "non_cli_backend"
    builder = plan["execution_roster"][0]
    assert builder["selection_reason"] == "configured eligible builder role"
    assert builder["assigned_task"] == "produce or revise the candidate implementation"
    assert rp.read_plan(tmp_path, run_uid=plan["run_uid"]) == plan


def test_declare_is_idempotent_but_conflicting_plan_fails_closed(tmp_path: Path) -> None:
    args = {
        "run_uid": "run-1",
        "seats": _seats(),
        "limits": rb.Limits.balanced(),
        "objective": "one",
        "created_ts": "2026-07-22T12:00:00Z",
    }
    first = rp.declare(tmp_path, **args)
    assert rp.declare(tmp_path, **args) == first
    with pytest.raises(rp.RunPlanError, match="conflicts"):
        rp.declare(tmp_path, **{**args, "objective": "two"})


def test_roster_projection_distinguishes_selection_transport_and_telemetry() -> None:
    plan = {
        "execution_roster": [
            {
                "role": "builder",
                "model": "gpt-5.6-luna",
                "configured": True,
                "eligible": True,
                "selected": True,
                "disabled_reason": None,
            },
            {
                "role": "reviewer",
                "model": "grok-4.5",
                "configured": True,
                "eligible": True,
                "selected": True,
                "disabled_reason": None,
            },
        ]
    }
    attempts = [
        {
            "seat": "builder",
            "requested_model": "gpt-5.6-luna",
            "source": "gateway_client",
            "state": "completed",
            "states": ["connecting", "gateway_accepted", "completed"],
            "telemetry_state": "telemetry_verified",
            "candidate_id": "candidate-a",
        },
        {
            "seat": "reviewer",
            "requested_model": "grok-4.5",
            "source": "orchestrator",
            "state": "failed",
            "states": ["queued", "starting", "failed"],
            "telemetry_state": None,
        },
    ]

    roster = rp.project_roster(plan, attempts)
    builder = roster[0]
    reviewer = roster[1]
    assert builder["invoked"] and builder["gateway_reached"] and builder["provider_returned"]
    assert builder["telemetry_reconciled"] and builder["state"] == "telemetry_reconciled"
    assert builder["evaluated"] is False
    assert builder["provider_attempt_count"] == 1
    assert builder["orchestration_execution_count"] == 0
    assert reviewer["queued"] and reviewer["invoked"]
    assert reviewer["provider_attempt_count"] == 0
    assert reviewer["orchestration_execution_count"] == 1
    assert not reviewer["gateway_reached"] and reviewer["failed_before_invocation"]
    assert reviewer["state"] == "failed_before_invocation"
    assert reviewer["terminal_disposition"] == "failed_before_invocation"

    evaluated = rp.project_roster(plan, attempts, evaluated_candidate_ids={"candidate-a"})
    assert evaluated[0]["evaluated"] is True
    assert evaluated[0]["terminal_disposition"] == "completed_and_evaluated"


def test_terminal_stop_reconciles_uninvoked_and_telemetry_failed_models() -> None:
    plan = {
        "execution_roster": [
            {
                "role": "builder",
                "model": "gpt-5.6-luna",
                "configured": True,
                "eligible": True,
                "selected": True,
            },
            {
                "role": "reviewer",
                "model": "grok-4.5",
                "configured": True,
                "eligible": True,
                "selected": True,
            },
        ]
    }
    attempts = [
        {
            "seat": "builder",
            "requested_model": "gpt-5.6-luna",
            "source": "gateway_client",
            "state": "completed",
            "states": ["connecting", "gateway_accepted", "completed"],
            "telemetry_state": "telemetry_failed",
            "candidate_id": "candidate-a",
        }
    ]
    decision = {
        "decision": "stop",
        "reason": "accepted_result_met_completion_criteria",
        "decision_id": "decision-1",
    }

    roster = rp.project_roster(
        plan,
        attempts,
        evaluated_candidate_ids={"candidate-a"},
        terminal_decision=decision,
    )

    assert roster[0]["telemetry_outcome"] == "failed"
    assert roster[0]["terminal_disposition"] == "completed_and_evaluated_telemetry_failed"
    assert roster[1]["state"] == "skipped"
    assert roster[1]["terminal_disposition"] == "skipped_after_terminal_decision"
    assert roster[1]["terminal_reason"] == "accepted_result_met_completion_criteria"


def test_autopilot_declares_plan_before_idle_exit_and_archive_replays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collab = tmp_path / "collab"
    hc.create(collab, to="human", from_="builder", title="owner decision", body="wait")
    seats = {
        "builder": {
            "backend": "cli",
            "model": "gpt-5.6-luna",
            "cmd": ["unused"],
            "system": "build",
        },
        "human": {"backend": "web"},
    }

    calls = ap.run(
        collab,
        seats=seats,
        runner=lambda *args, **kwargs: pytest.fail("idle run dispatched a seat"),
        home=tmp_path,
        objective="Wait for explicit owner decision",
    )

    assert calls == 0
    status = dc.read_status(collab)
    assert status is not None
    run_uid = status["run_uid"]
    plan = rp.read_plan(collab, run_uid=run_uid)
    assert plan is not None and plan["objective"] == "Wait for explicit owner decision"
    detail = dc.run_detail(collab, run_uid)
    assert detail["plan"] == plan
    assert [item["state"] for item in detail["roster"]] == ["selected", "disabled"]
    assert detail["manifest"]["state"] == "sealed"
    assert detail["evidence_health"]["archive_integrity"] == "healthy"
    run_dir = collab / "autopilot" / "history" / run_uid
    assert rm.verify(run_dir) == {
        "valid": True,
        "state": "sealed",
        "run_uid": run_uid,
        "failures": [],
        "gaps": [],
    }

    monkeypatch.setattr(dc, "driver_running", lambda _collab: {"run_uid": run_uid})
    live = dc.snapshot(collab, home=tmp_path)
    assert live["run_plan"] == plan
    assert live["execution_roster"] == detail["roster"]
