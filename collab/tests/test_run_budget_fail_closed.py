"""Tests for run_budget hardening (ADR-0003 Phase 1).

Pins the fail-closed contract added on top of the ADR-0002 budget module:
  * a corrupt/torn record **refuses** rather than silently resetting the counters (a reset
    would be a budget bypass);
  * an absent record still bootstraps fresh (normal first use);
  * atomic charging never overspends under concurrent lanes;
  * a charge is spent unconditionally (no refund path — a failed call still costs);
  * `Limits.balanced()` carries the calibrated defaults;
  * `max_findings_per_lane` is enforced (overflow is surfaced, never dropped).
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import run_budget as rb  # noqa: E402


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


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


def _budget(tmp_path, limits=None, clock=None) -> rb.RunBudget:
    return rb.RunBudget(str(tmp_path), "029", limits or _limits(), wall_clock=clock or _FakeClock())


class TestFailClosedLoad:
    def test_absent_record_bootstraps_fresh(self, tmp_path):
        b = _budget(tmp_path)
        assert b.epoch == 0
        assert b.consumed()["work_attempts"] == 0

    def test_corrupt_record_refuses(self, tmp_path):
        # Establish a real budget file, then corrupt it (a torn write / bad byte).
        b = _budget(tmp_path)
        b.charge(rb.WORK_ATTEMPT)
        path = tmp_path / "autopilot" / "budget" / f"{cc.slugify('029')}.json"
        assert path.exists()
        path.write_text("{ this is not json", encoding="utf-8")

        # A fresh instance over the same handoff must NOT silently reset — it refuses.
        with pytest.raises(cc.CollabError):
            _budget(tmp_path)

    def test_corrupt_record_does_not_reset_counters(self, tmp_path):
        # The bypass we are preventing: a corrupt record must never re-grant a full allowance.
        b = _budget(tmp_path)
        for _ in range(b.limits.max_work_attempts):
            b.charge(rb.WORK_ATTEMPT)
        assert b.exhausted() == "work_attempts"
        path = tmp_path / "autopilot" / "budget" / f"{cc.slugify('029')}.json"
        path.write_text("", encoding="utf-8")  # truncated / empty == torn
        with pytest.raises(cc.CollabError):
            _budget(tmp_path)

    def test_valid_record_reloads(self, tmp_path):
        b = _budget(tmp_path)
        b.charge(rb.WORK_ATTEMPT)
        b.charge(rb.WORK_ATTEMPT)
        again = _budget(tmp_path)
        assert again.consumed()["work_attempts"] == 2


class TestAtomicUnderConcurrency:
    def test_never_overspends_total_model_calls(self, tmp_path):
        limit = 25
        b = _budget(tmp_path, _limits(max_total_model_calls=limit, max_findings_per_lane=999))
        granted = 0

        def one():
            nonlocal granted
            try:
                b.charge(rb.VERIFICATION_CALL)
                return True
            except rb.BudgetExceeded:
                return False

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: one(), range(200)))
        granted = sum(1 for r in results if r)
        assert granted == limit
        assert b.consumed()["total_model_calls"] == limit


class TestNoRefund:
    def test_charge_is_spent_unconditionally(self, tmp_path):
        # A charge models cost already incurred; there is no refund/rollback path, so a failed
        # downstream call cannot be reclaimed to farm free retries (ADR-0002 D6).
        b = _budget(tmp_path)
        b.charge(rb.WORK_ATTEMPT)
        assert b.consumed()["work_attempts"] == 1
        assert not hasattr(b, "refund")
        assert not hasattr(b, "rollback")

    def test_denied_charge_leaves_state_unchanged(self, tmp_path):
        b = _budget(tmp_path, _limits(max_work_attempts=1))
        b.charge(rb.WORK_ATTEMPT)
        before = b.consumed()
        with pytest.raises(rb.BudgetExceeded):
            b.charge(rb.WORK_ATTEMPT)
        assert b.consumed() == before


class TestBalancedDefaults:
    def test_balanced_profile(self):
        lim = rb.Limits.balanced()
        assert lim.max_work_attempts == 3
        assert lim.max_verification_passes == 6
        assert lim.max_total_model_calls == 18
        assert lim.max_wall_clock_seconds == 1800.0
        assert lim.max_findings_per_lane == 3
        assert lim.max_review_decisions_per_candidate == 1


class TestFindingsCap:
    def test_under_cap_verifies_all_no_overflow(self, tmp_path):
        b = _budget(tmp_path, _limits(max_findings_per_lane=4))
        split = b.cap_lane_findings(3)
        assert split == {"verify": 3, "overflow": 0, "cap": 4}

    def test_over_cap_surfaces_overflow(self, tmp_path):
        b = _budget(tmp_path, _limits(max_findings_per_lane=4))
        split = b.cap_lane_findings(7)
        assert split == {"verify": 4, "overflow": 3, "cap": 4}

    def test_zero_and_negative_are_safe(self, tmp_path):
        b = _budget(tmp_path, _limits(max_findings_per_lane=4))
        assert b.cap_lane_findings(0) == {"verify": 0, "overflow": 0, "cap": 4}
        assert b.cap_lane_findings(-5) == {"verify": 0, "overflow": 0, "cap": 4}
