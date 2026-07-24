"""Structured continue/stop decision records and exact reason precedence."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import round_decision as rd  # noqa: E402


def _context(**changes) -> rd.DecisionContext:
    base = rd.DecisionContext(
        run_uid="run-1",
        completed_round=2,
        maximum_rounds=3,
        candidate_id="cand:abc",
        unresolved_requirements=("R-1",),
        viable_models=("haiku-4.5",),
        remaining_budget={"tokens": None, "time_seconds": 120, "cost": None},
        timestamp="2026-07-22T12:00:00Z",
        supporting_evidence=("assessment:cand:abc",),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"completed_round": 3, "maximum_rounds": 3}, "round_cap_reached"),
        (
            {"accepted": True, "completion_criteria_met": True, "unresolved_requirements": ()},
            "accepted_result_met_completion_criteria",
        ),
        ({"further_rounds_unnecessary": True}, "evaluator_judged_further_rounds_unnecessary"),
        ({"viable_candidates": 0}, "no_viable_candidates_remained"),
        ({"all_remaining_candidates_rejected": True}, "all_remaining_candidates_rejected"),
        ({"duplicate_or_non_improving": True}, "duplicate_or_non_improving_output"),
        ({"convergence_threshold_reached": True}, "convergence_threshold_reached"),
        ({"budget_exhausted": True}, "budget_exhausted"),
        ({"timeout_reached": True}, "timeout_reached"),
        ({"user_cancelled": True}, "user_cancellation"),
        ({"required_dependency_unavailable": True}, "required_dependency_unavailable"),
        ({"model_failure_threshold_reached": True}, "model_failure_threshold_reached"),
        ({"policy_or_safety_rejection": True}, "policy_or_safety_rejection"),
        ({"orchestration_error": True}, "orchestration_error"),
    ],
)
def test_every_required_stop_reason_is_structured(changes: dict, reason: str) -> None:
    decision = rd.evaluate(_context(**changes))
    assert decision["decision"] == "stop"
    assert decision["reason"] == reason
    assert len(decision["rules_evaluated"]) == len(rd.STOP_REASONS)
    fired = {rule["rule"] for rule in decision["rules_evaluated"] if rule["result"]}
    assert reason in fired
    assert any(not rule["result"] for rule in decision["rules_evaluated"])


def test_required_dependency_failure_precedes_round_cap_for_observed_timeout() -> None:
    decision = rd.evaluate(
        _context(
            completed_round=3,
            maximum_rounds=3,
            required_dependency_unavailable=True,
            timeout_reached=True,
        )
    )
    assert decision["reason"] == "required_dependency_unavailable"
    assert decision["decision"] == "stop"


def test_continue_record_has_positive_path_and_negative_stop_rules() -> None:
    decision = rd.evaluate(_context())
    assert decision["decision"] == "continue"
    assert decision["reason"] == "unresolved_requirements_and_viable_path"
    assert decision["unresolved_requirements"] == ["R-1"]
    assert decision["remaining_viable_models"] == ["haiku-4.5"]
    assert not any(rule["result"] for rule in decision["rules_evaluated"])


def test_persist_is_idempotent_and_conflicting_decision_id_fails_closed(tmp_path: Path) -> None:
    decision = rd.evaluate(_context())
    rd.persist(tmp_path, decision)
    rd.persist(tmp_path, decision)
    assert rd.read_decisions(tmp_path, run_uid="run-1") == [decision]

    conflict = {**decision, "reason": "orchestration_error"}
    with pytest.raises(rd.RoundDecisionError, match="collision"):
        rd.persist(tmp_path, conflict)
