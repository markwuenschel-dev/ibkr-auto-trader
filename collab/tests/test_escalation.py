"""Tests for the auto-fix-once-then-escalate policy and the escalation artifact.

Policy (user-chosen): a lane-CONFIRMED defect gets ONE informed autonomous builder fix attempt; if the lanes
still confirm a defect, the driver STOPS and writes an escalation to the terminal instead of thrashing to the
round cap. These tests pin: the escalation module (render/write/read/pending/clear), the driver helpers
(_confirmed_blockers / _fix_directive), and the end-to-end loop policy (one fix attempt, then escalate; and
that a block with NO confirmed lane defect does NOT escalate).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import escalation  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402

_BLOCKER = {"id": "b1", "lane": "clock", "fixed": False, "regression_test": "test_skew_naive",
            "description": "tz-aware/naive TypeError at gateway.py:233 gates the snapshot read"}


def _events(collab):
    p = Path(collab) / "logs" / "events.jsonl"
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p.exists() else []


def _ledger_with_blocker(collab, hid, blockers):
    lanes.write_ledger(collab, hid, {
        "hid": hid, "generated_ts": ap._now_utc(), "guardrails": [],
        "builder_seat": "builder", "reviewer_seat": "reviewer", "lanes": [], "blockers": blockers})


class TestEscalationModule:
    def test_render_lists_defects_and_repro(self):
        md = escalation.render("028", [_BLOCKER], attempts=1, title="PT-3 gateway", run_uid="R1")
        assert md.startswith("<!-- escalation:028 -->")
        assert "needs a terminal fix: 028 · PT-3 gateway" in md
        assert "1 autonomous fix attempt" in md and "CONFIRMED **1 defect**" in md
        assert "gateway.py:233" in md and "test_skew_naive" in md
        assert md.rstrip().endswith("<!-- /escalation:028 -->")

    def test_write_read_pending_clear_roundtrip(self, tmp_path):
        collab = str(tmp_path / "c")
        p = escalation.write(collab, "028", [_BLOCKER], attempts=1)
        assert Path(p).exists() and escalation.pending(collab) == ["028"]
        assert "gateway.py:233" in escalation.read(collab, "028")["markdown"]
        assert escalation.clear(collab, "028") is True
        assert escalation.pending(collab) == [] and escalation.read(collab, "028") is None

    def test_render_plural_and_zero_grammar(self):
        assert "0 defects" in escalation.render("x", [], attempts=2)
        assert "2 autonomous fix attempts" in escalation.render("x", [], attempts=2)


class TestDriverHelpers:
    def test_confirmed_blockers_reads_ledger(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        _ledger_with_blocker(collab, "001", [_BLOCKER])
        got = ap._confirmed_blockers(collab, "001")
        assert len(got) == 1 and got[0]["id"] == "b1"

    def test_confirmed_blockers_empty_without_ledger(self, tmp_path):
        assert ap._confirmed_blockers(str(tmp_path / "c"), "999") == []

    def test_fix_directive_names_the_defects(self):
        d = ap._fix_directive([_BLOCKER])
        assert "VERIFIED DEFECTS" in d and "gateway.py:233" in d and "test_skew_naive" in d


def _seats():
    # builder acts first (handoff to:builder); reviewer is the sign-off authority.
    return {"builder": {"backend": "cli", "cmd": ["fake-builder"], "system": "build"},
            "reviewer": {"backend": "cli", "cmd": ["fake-reviewer"], "system": "review",
                         "can_sign_off": True}}


class TestLoopPolicy:
    def test_auto_fix_once_then_escalate(self, tmp_path):
        collab, home = str(tmp_path / "c"), str(tmp_path)
        hc.create(collab, to="builder", from_="reviewer", title="PT-3 gateway", body="build it")
        _ledger_with_blocker(collab, "001", [_BLOCKER])  # a persistent confirmed defect (never cleared)

        # reviewer always signs off; builder just "works". The ledger's blocker never clears, so every
        # sign-off is refused -> 1 informed fix attempt, then escalate.
        def fake(cmd, prompt, **k):
            return "[[SIGNOFF]]" if "reviewer" in cmd[0] else "did the work"

        ap.run(collab, seats=_seats(), max_rounds=8, runner=fake, home=home)

        assert escalation.pending(collab) == ["001"]                      # escalated
        md = escalation.read(collab, "001")["markdown"]
        assert "gateway.py:233" in md and "PT-3 gateway" in md
        evs = _events(collab)
        assert any(e["stage"] == "autopilot.escalation" for e in evs)
        # exactly ONE informed fix attempt was recorded before escalating
        esc = next(e for e in evs if e["stage"] == "autopilot.escalation")
        assert "fix_attempts:1" in (esc["decision"]["reason_codes"])
        assert hc.state_of(collab, "001") == "claimed"                    # not shipped

    def test_block_without_confirmed_defect_does_not_escalate(self, tmp_path):
        collab, home = str(tmp_path / "c"), str(tmp_path)
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        # NO ledger -> sign-off is blocked (contract unsatisfied) but there is no CONFIRMED lane defect,
        # so the policy must NOT escalate — it just runs the exchange to the cap.
        def fake(cmd, prompt, **k):
            return "[[SIGNOFF]]" if "reviewer" in cmd[0] else "working"

        ap.run(collab, seats=_seats(), max_rounds=4, runner=fake, home=home)
        assert escalation.pending(collab) == []
        assert not any(e["stage"] == "autopilot.escalation" for e in _events(collab))
