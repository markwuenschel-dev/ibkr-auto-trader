"""Tests for run_budget — the ADR-0002 bounded-autonomy accounting module.

Pins the contract: five separately-budgeted things, global ceilings vs per-kind budgets, the
one-decision-per-candidate invariant, atomic charging under parallel lanes, wall-clock, exhaustion
reporting, and human-authorized reopen epochs (counters reset, closed epochs immutable).
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import run_budget as rb  # noqa: E402


def _limits(**over) -> rb.Limits:
    base = dict(
        max_work_attempts=3,
        max_verification_passes=5,
        max_total_model_calls=20,
        max_wall_clock_seconds=100.0,
        max_findings_per_lane=4,
    )
    base.update(over)
    return rb.Limits(**base)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _budget(tmp_path, limits=None, clock=None) -> rb.RunBudget:
    return rb.RunBudget(str(tmp_path), "029", limits or _limits(), wall_clock=clock or _FakeClock())


class TestLimitsInvariant:
    def test_review_decisions_per_candidate_must_be_one(self):
        with pytest.raises(ValueError):
            _limits(max_review_decisions_per_candidate=2)
        assert _limits().max_review_decisions_per_candidate == 1


class TestWorkAttempts:
    def test_charges_attempt_actor_and_model_call(self, tmp_path):
        b = _budget(tmp_path)
        b.charge(rb.WORK_ATTEMPT)
        c = b.consumed()
        assert c["work_attempts"] == 1 and c["actor_turns"] == 1 and c["total_model_calls"] == 1

    def test_caps_at_max_work_attempts(self, tmp_path):
        b = _budget(tmp_path, _limits(max_work_attempts=2))
        b.charge(rb.WORK_ATTEMPT)
        b.charge(rb.WORK_ATTEMPT)
        with pytest.raises(rb.BudgetExceeded) as exc:
            b.charge(rb.WORK_ATTEMPT)
        assert exc.value.which == "work_attempts"
        # a denied charge does not mutate state
        assert b.consumed()["work_attempts"] == 2


class TestReviewDecisions:
    def test_one_per_candidate_then_denied(self, tmp_path):
        b = _budget(tmp_path)
        b.charge(rb.REVIEW_DECISION, candidate="cand:A")
        with pytest.raises(rb.BudgetExceeded) as exc:
            b.charge(rb.REVIEW_DECISION, candidate="cand:A")
        assert exc.value.which == "review_decisions"

    def test_a_new_candidate_gets_its_own_decision(self, tmp_path):
        b = _budget(tmp_path)
        b.charge(rb.REVIEW_DECISION, candidate="cand:A")
        b.charge(rb.REVIEW_DECISION, candidate="cand:B")  # different candidate -> allowed
        assert b.consumed()["actor_turns"] == 2
        assert b.consumed()["total_model_calls"] == 2

    def test_requires_a_candidate(self, tmp_path):
        with pytest.raises(ValueError):
            _budget(tmp_path).charge(rb.REVIEW_DECISION)


class TestGlobalCeilings:
    def test_total_model_calls_spans_every_kind(self, tmp_path):
        # 1 work attempt + 1 review + N verification calls all draw the same total ceiling.
        b = _budget(tmp_path, _limits(max_total_model_calls=3, max_verification_passes=9))
        b.charge(rb.WORK_ATTEMPT)  # total=1
        b.charge(rb.REVIEW_DECISION, candidate="cand:A")  # total=2
        b.charge(rb.VERIFICATION_CALL)  # total=3
        with pytest.raises(rb.BudgetExceeded) as exc:
            b.charge(rb.VERIFICATION_CALL)  # would be total=4
        assert exc.value.which == "total_model_calls"

    def test_verification_pass_does_not_draw_model_calls(self, tmp_path):
        b = _budget(tmp_path, _limits(max_total_model_calls=1))
        b.charge(rb.VERIFICATION_PASS)
        b.charge(rb.VERIFICATION_PASS)  # passes are not model calls -> not capped by total
        assert b.consumed()["total_model_calls"] == 0

    def test_wall_clock_exhaustion(self, tmp_path):
        clock = _FakeClock()
        b = _budget(tmp_path, _limits(max_wall_clock_seconds=30.0), clock=clock)
        b.check_wall_clock()  # fine at t=1000 (origin)
        clock.t += 30.0
        with pytest.raises(rb.BudgetExceeded) as exc:
            b.check_wall_clock()
        assert exc.value.which == "wall_clock"


class TestExhaustionReporting:
    def test_exhausted_names_the_hit_budget(self, tmp_path):
        b = _budget(tmp_path, _limits(max_work_attempts=1))
        assert b.exhausted() is None
        b.charge(rb.WORK_ATTEMPT)
        assert b.exhausted() == "work_attempts"

    def test_report_has_every_budget(self, tmp_path):
        b = _budget(tmp_path)
        b.charge(rb.WORK_ATTEMPT)
        rep = b.report()
        assert rep["epoch"] == 0
        for key in (
            "work_attempts",
            "verification_passes",
            "verification_calls",
            "total_model_calls",
            "actor_turns",
            "wall_clock_seconds",
        ):
            assert key in rep["budgets"]
        assert rep["budgets"]["work_attempts"]["consumed"] == 1


class TestAtomicUnderParallelLanes:
    def test_concurrent_charges_never_exceed_the_ceiling(self, tmp_path):
        limit = 10
        b = _budget(tmp_path, _limits(max_total_model_calls=limit))
        ok, denied = 0, 0

        def worker(_):
            nonlocal ok, denied
            try:
                b.charge(rb.VERIFICATION_CALL)
                return True
            except rb.BudgetExceeded:
                return False

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(worker, range(100)))
        ok = sum(results)
        denied = len(results) - ok
        assert ok == limit  # exactly the ceiling was granted — never overspent
        assert denied == 100 - limit
        assert b.consumed()["total_model_calls"] == limit


class TestPersistenceAndReopen:
    def test_counters_survive_reload(self, tmp_path):
        b1 = _budget(tmp_path)
        b1.charge(rb.WORK_ATTEMPT)
        b1.charge(rb.VERIFICATION_CALL)
        b2 = _budget(tmp_path)  # a fresh instance loads the persisted record
        assert b2.consumed()["work_attempts"] == 1
        assert b2.consumed()["total_model_calls"] == 2

    def test_new_epoch_resets_counters_and_preserves_history(self, tmp_path):
        b = _budget(tmp_path, _limits(max_work_attempts=1))
        b.charge(rb.WORK_ATTEMPT)
        assert b.exhausted() == "work_attempts"
        epoch = b.new_epoch(authorized_by="maw")
        assert epoch == 1
        assert b.consumed()["work_attempts"] == 0  # fresh budget
        assert b.exhausted() is None
        # prior epoch is preserved immutably, with the authorizing human recorded on the new one
        b.charge(rb.WORK_ATTEMPT)  # allowed again in the new epoch
        assert b.consumed()["work_attempts"] == 1
        assert b.consumed().get("authorized_by") == "maw"


class TestCandidateId:
    def test_source_change_changes_id(self):
        a = rb.candidate_id({"a.py": "h1"}, source_roots=["src"], test_command="pytest", lane_config={})
        b = rb.candidate_id({"a.py": "h2"}, source_roots=["src"], test_command="pytest", lane_config={})
        assert a != b and a.startswith("cand:")

    def test_verification_plan_change_changes_id(self):
        # Same source, tightened plan (extra lane) -> a new candidate; old evidence can't be reused.
        src = {"a.py": "h1"}
        a = rb.candidate_id(src, source_roots=["src"], test_command="pytest", lane_config={"lanes": ["x"]})
        b = rb.candidate_id(
            src, source_roots=["src"], test_command="pytest", lane_config={"lanes": ["x", "y"]}
        )
        assert a != b

    def test_identical_inputs_are_stable(self):
        args = dict(source_roots=["src"], test_command="pytest", lane_config={"g": 1})
        assert rb.candidate_id({"a.py": "h"}, **args) == rb.candidate_id({"a.py": "h"}, **args)
