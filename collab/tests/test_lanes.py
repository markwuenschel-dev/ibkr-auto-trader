"""Tests for lanes.py — the adversarial-lane runner (ARCHITECTURE.md §18.2, autonomous revision).

The breaker/verifier backends are injected (``runner=``) so the whole pipeline runs with FAKE agents —
no real CLI, no network. What's tested: the breaker→refute pipeline, the default-REJECTED verifier, the
ledger, and the structural independence rule (no seat verifies its own work).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402


def _seats(names):
    return {n: {"backend": "cli", "cmd": [f"fake-{n}"], "system": f"You are {n}."} for n in names}


def _handoff(collab, *, frm="builder", to="reviewer", body="the change under test"):
    hc.create(collab, to=to, from_=frm, title="please review", body=body)
    return "001"


# A fake backend routed by the seat's cmd (fake-<seat>): breaker seats emit findings, verifier seats vote.
def _fake(breaker_out, verifier_out):
    def run(cmd, prompt, *, timeout, **kw):  # **kw tolerates unset_env passed by run_lane
        who = cmd[0]
        if "breaker" in who or who.endswith("-b"):
            return breaker_out
        return verifier_out
    return run


class TestRequiredLanes:
    def test_v2_compatibility_selector_returns_matching_baseline_contracts(self):
        cfg = lanes.load_lanes()  # the shipped telemetry/lanes.json
        got = lanes.required_lanes(
            ["path-safety", "data-integrity", "bounded-autonomy", "untrusted-agent-output"], cfg)
        assert set(got) == {
            "untrusted-agent-output", "bounded-autonomy", "path-pointer-safety",
            "change-regression", "data-integrity-under-concurrent-autopilots"}

    def test_no_guardrails_requires_no_lanes(self):
        assert lanes.required_lanes([], lanes.load_lanes()) == []


class TestRunLane:
    def test_clean_lane_when_breaker_finds_nothing(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        r = lanes.run_lane(collab, "001", "untrusted-agent-output",
                           seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
                           builder_seat="builder", runner=_fake("NO-FINDING", "unused"))
        assert r["ran"] is True and r["confirmed"] == [] and r["refuted"] == []

    def test_confirmed_finding_survives(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        r = lanes.run_lane(collab, "001", "path-pointer-safety",
                           seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
                           builder_seat="builder",
                           runner=_fake("FINDING: _substance -> ../ escapes replies dir",
                                        "VERDICT: CONFIRMED _substance ../ escape reproduces"))
        assert r["confirmed"] == ["_substance -> ../ escapes replies dir"] and r["refuted"] == []

    def test_verifier_refutes_by_default(self, tmp_path):
        # A finding whose verifier does NOT emit CONFIRMED must be REJECTED, not confirmed.
        collab = str(tmp_path / "c")
        _handoff(collab)
        r = lanes.run_lane(collab, "001", "bounded-autonomy",
                           seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
                           builder_seat="builder",
                           runner=_fake("FINDING: maybe unbounded?", "I am not sure; looks fine actually."))
        assert r["confirmed"] == [] and r["refuted"] == ["maybe unbounded?"]

    def test_independence_violation_raises(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        with pytest.raises(cc.CollabError, match="independence"):
            lanes.run_lane(collab, "001", "x", seats=_seats(["builder", "v"]),
                           breaker_seat="builder", verifier_seat="v",  # breaker == builder
                           builder_seat="builder", runner=_fake("NO-FINDING", "x"))


class TestRunLanes:
    def test_writes_ledger_with_blockers_from_confirmed(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab, frm="builder")
        ledger = lanes.run_lanes(
            collab, "001", seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
            builder_seat="builder",
            guardrails=["bounded-autonomy", "untrusted-agent-output"],  # -> baseline + matching generic contracts
            runner=_fake("FINDING: X -> concrete trigger", "VERDICT: CONFIRMED X trigger"))
        # ledger on disk, one blocker per (lane, confirmed finding)
        on_disk = lanes.read_ledger(collab, "001")
        assert on_disk == ledger
        assert lanes.ledger_path(collab, "001").exists()
        assert {b["lane"] for b in ledger["blockers"]} == {
            "change-regression", "untrusted-agent-output", "bounded-autonomy"}
        assert all(b["fixed"] is False and b["regression_test"] is None for b in ledger["blockers"])
        assert ledger["builder_seat"] == "builder" and ledger["reviewer_seat"] == "v"

    def test_clean_run_has_no_blockers(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        ledger = lanes.run_lanes(
            collab, "001", seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
            builder_seat="builder", guardrails=["bounded-autonomy", "untrusted-agent-output"],
            source_roots=None, source_base=None, runner=_fake("NO-FINDING", "unused"))
        assert ledger["blockers"] == []
        assert len(ledger["lanes"]) == 3 and all(x["ran"] for x in ledger["lanes"])

    def test_ledger_carries_source_manifest_when_roots_given(self, tmp_path):
        collab = tmp_path / "c"
        _handoff(str(collab))
        (collab / "src").mkdir(parents=True, exist_ok=True)
        (collab / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
        ledger = lanes.run_lanes(
            str(collab), "001", seats=_seats(["b", "v"]), breaker_seat="b", verifier_seat="v",
            builder_seat="builder", guardrails=["bounded-autonomy", "untrusted-agent-output"],
            source_roots=["src/*.py"], source_base=str(collab), runner=_fake("NO-FINDING", "u"))
        assert "src/m.py" in ledger["source_manifest"]
