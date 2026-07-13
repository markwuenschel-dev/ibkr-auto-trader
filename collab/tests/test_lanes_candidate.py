"""Tests for the ADR-0003 Phase 5 lane additions: per-candidate immutable ledgers, budget charging,
the findings cap, and structured tool-failure capture. Backends are injected (fake agents).
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import run_budget as rb  # noqa: E402


def _seats(names):
    return {n: {"backend": "cli", "cmd": [f"fake-{n}"], "system": f"You are {n}."} for n in names}


def _handoff(collab, *, frm="builder", to="reviewer"):
    hc.create(collab, to=to, from_=frm, title="please review", body="change under test")
    return "001"


def _fake(breaker_out, verifier_out):
    def run(cmd, prompt, *, timeout, **kw):
        return breaker_out if "breaker" in cmd[0] or cmd[0].endswith("-b") else verifier_out
    return run


def _budget(tmp_path, **over):
    base = dict(max_work_attempts=3, max_verification_passes=5, max_total_model_calls=100,
                max_wall_clock_seconds=1000.0, max_findings_per_lane=2)
    base.update(over)
    return rb.RunBudget(str(tmp_path / "b"), "001", rb.Limits(**base))


class TestPerCandidateLedger:
    def test_written_to_candidate_path_and_reused(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        calls = {"n": 0}

        def counting(cmd, prompt, *, timeout, **kw):
            calls["n"] += 1
            return "NO-FINDING" if "breaker" in cmd[0] else "VERDICT: REFUTED"

        led1 = lanes.run_lanes(collab, "001", seats=_seats(["b", "v"]), breaker_seat="b",
                               verifier_seat="v", builder_seat="builder",
                               guardrails=["path-safety"], runner=counting, candidate_id="cand:abc")
        assert led1["candidate_id"] == "cand:abc"
        p = lanes.ledger_path(collab, "001", "cand:abc")
        assert p.exists() and "cand-abc" in p.name
        after_first = calls["n"]
        # Re-running the IDENTICAL candidate reuses the immutable ledger — zero new backend calls.
        led2 = lanes.run_lanes(collab, "001", seats=_seats(["b", "v"]), breaker_seat="b",
                               verifier_seat="v", builder_seat="builder",
                               guardrails=["path-safety"], runner=counting, candidate_id="cand:abc")
        assert calls["n"] == after_first
        assert led2 == led1

    def test_legacy_path_still_used_without_candidate(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        lanes.run_lanes(collab, "001", seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
                        builder_seat="builder", guardrails=["path-safety"],
                        runner=_fake("NO-FINDING", "x"))
        assert lanes.ledger_path(collab, "001").exists()  # legacy per-handoff file


class TestBudgetCharging:
    def test_breaker_and_verifier_calls_charged(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        budget = _budget(tmp_path)
        lanes.run_lane(collab, "001", "path-pointer-safety", seats=_seats(["b", "v"]),
                       breaker_seat="b", verifier_seat="v", builder_seat="builder",
                       runner=_fake("FINDING: x -> boom", "VERDICT: REFUTED"), budget=budget)
        # one breaker + one verifier
        assert budget.consumed()["verification_calls"] == 2

    def test_budget_exhaustion_marks_incomplete(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        budget = _budget(tmp_path, max_total_model_calls=1)  # only the breaker fits
        r = lanes.run_lane(collab, "001", "path-pointer-safety", seats=_seats(["b", "v"]),
                           breaker_seat="b", verifier_seat="v", builder_seat="builder",
                           runner=_fake("FINDING: a -> x\nFINDING: b -> y", "VERDICT: CONFIRMED a x"),
                           budget=budget)
        assert r["ran"] is False and "incomplete" in r


class TestFindingsCap:
    def test_over_cap_findings_surface_overflow(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        budget = _budget(tmp_path, max_findings_per_lane=2)
        r = lanes.run_lane(collab, "001", "path-pointer-safety", seats=_seats(["b", "v"]),
                           breaker_seat="b", verifier_seat="v", builder_seat="builder",
                           runner=_fake("FINDING: a -> x\nFINDING: b -> y\nFINDING: c -> z",
                                        "VERDICT: REFUTED"), budget=budget)
        assert r["overflow"] == 1
        assert len(r["unverified"]) == 1
        assert len(r["confirmed"]) + len(r["refuted"]) == 2  # only the cap was verified

    def test_run_lanes_aggregates_incomplete(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        budget = _budget(tmp_path, max_findings_per_lane=1)
        led = lanes.run_lanes(collab, "001", seats=_seats(["b", "v"]), breaker_seat="b",
                              verifier_seat="v", builder_seat="builder", guardrails=["path-safety"],
                              runner=_fake("FINDING: a -> x\nFINDING: b -> y", "VERDICT: REFUTED"),
                              budget=budget, candidate_id="cand:over")
        assert led["incomplete"] is True and led["overflow"] >= 1


class TestToolFailureCapture:
    def test_backend_nonzero_exit_becomes_tool_error(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)

        def crashing(cmd, prompt, *, timeout, **kw):
            raise cc.CollabError("backend 'python' exited 2: unrecognized arguments --permission-mode")

        led = lanes.run_lanes(collab, "001", seats=_seats(["b", "v"]), breaker_seat="b",
                              verifier_seat="v", builder_seat="builder", guardrails=["path-safety"],
                              runner=crashing, candidate_id="cand:crash")
        assert led["tool_error"] is not None
        assert "exited 2" in led["tool_error"]["error"]
        # a crashed lane does not manufacture a clean pass
        assert led["blockers"] == []
