"""Tests for the auto-fix-once-then-escalate policy and the escalation artifact.

Policy (user-chosen): a lane-CONFIRMED defect gets ONE informed autonomous builder fix attempt; if the lanes
still confirm a defect, the driver STOPS and writes an escalation to the terminal instead of thrashing to the
round cap. These tests pin: the escalation module (render/write/read/pending/clear), the driver helpers
(_confirmed_blockers / _fix_directive), and the end-to-end loop policy (one fix attempt, then escalate; and
that a block with NO confirmed lane defect does NOT escalate).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import escalation  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import run_budget as rb  # noqa: E402

# The gate fixture lives with the driver tests: one definition of "a source tree with a real
# authoritative gate", so both suites exercise the same discovered-and-canonical path.
from test_autopilot import _gate_repo  # noqa: E402

import conftest  # noqa: E402  — shared v2 assurance catalog (ADR-0005)

_BLOCKER = {
    "id": "b1",
    "lane": "clock",
    "fixed": False,
    "regression_test": "test_skew_naive",
    "description": "tz-aware/naive TypeError at gateway.py:233 gates the snapshot read",
}


def _events(collab):
    p = Path(collab) / "logs" / "events.jsonl"
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p.exists() else []


def _ledger_with_blocker(collab, hid, blockers):
    lanes.write_ledger(
        collab,
        hid,
        {
            "hid": hid,
            "generated_ts": ap._now_utc(),
            "guardrails": [],
            "builder_seat": "builder",
            "reviewer_seat": "reviewer",
            "lanes": [],
            "blockers": blockers,
        },
    )


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

    def test_a_tool_failure_is_not_reported_as_a_verified_defect(self):
        # The 030 case: the lanes never finished because a seat process was SIGHUP'd. Zero findings.
        # Reporting that as "⚠ Verified defect — needs a terminal fix" sends a human to hunt a bug that
        # does not exist, in code no lane ever finished checking.
        cause = {"lane": "change-regression", "seat": "verifier", "error": "backend 'claude' exited 129"}
        md = escalation.render(
            "030", [], attempts=1, reason="infrastructure_blocked", cause=cause, run_uid="R9"
        )
        assert "Verified defect" not in md
        assert "Stopped by a TOOL failure — no defect was confirmed: 030" in md
        assert "NOT a code defect" in md
        assert "backend 'claude' exited 129" in md and "change-regression" in md
        assert "does NOT re-run the builder" in md  # the clearing path is adopt, not a rebuild

    def test_a_confirmed_defect_still_reads_as_one(self):
        md = escalation.render("028", [_BLOCKER], attempts=1, reason="budget_exhausted")
        assert "⚠ Verified defect — needs a terminal fix: 028" in md
        assert "CONFIRMED **1 defect**" in md and "NOT a code defect" not in md


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
    return {
        "builder": {"backend": "cli", "cmd": ["fake-builder"], "system": "build"},
        "reviewer": {"backend": "cli", "cmd": ["fake-reviewer"], "system": "review", "can_sign_off": True},
    }


def _closeout_seats():
    return {
        "builder": {"backend": "cli", "cmd": ["fake-builder"], "system": "b"},
        "grok": {"backend": "cli", "cmd": ["fake-grok"], "system": "breaker"},
        "gemini": {"backend": "cli", "cmd": ["fake-gemini"], "system": "verifier"},
        "reviewer": {"backend": "cli", "cmd": ["fake-reviewer"], "system": "r", "can_sign_off": True},
    }


def _bounded_limits(work_attempts=2):
    # A generous model-call ceiling so a multi-attempt lane run is bounded by the work-attempt budget only.
    return rb.Limits(
        max_work_attempts=work_attempts,
        max_verification_passes=8,
        max_total_model_calls=500,
        max_wall_clock_seconds=1800.0,
        max_findings_per_lane=4,
    )


class TestLoopPolicy:
    """ADR-0003 pause semantics: a candidate that cannot be approved is retried per WORK_ATTEMPT until the
    budget is exhausted (or a fix makes no progress), then a durable escalation is written — never a thrash
    to a silent stop, never a ship on an unsatisfied contract."""

    def _lane_slice(self, collab, home, tmp_path, runner):
        (Path(collab) / "src").mkdir(parents=True, exist_ok=True)
        (Path(collab) / "src" / "gateway.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=collab, capture_output=True)  # cond-11 preflight needs a repo
        # Typed constraints (ADR-0005): conformance is adjudicated against declared ids, and a handoff
        # with none is refused before any model work.
        hc.create(
            collab,
            to="builder",
            from_="reviewer",
            title="PT-3 gateway",
            body="build it",
            constraints=[("C1", "the gateway module exports x")],
        )
        import re as _re

        _, p = hc._reconcile(collab, "001")
        txt = _re.sub(
            r"(?m)^(status:.*\n)",
            r"\1guardrails: [bounded-autonomy, untrusted-agent-output]\n",
            Path(p).read_text("utf-8"),
            count=1,
        )
        Path(p).write_text(txt, "utf-8")
        tiny = tmp_path / "test_tiny.py"
        tiny.write_text("def test_x():\n    assert True\n", encoding="utf-8")
        # The authoritative whole-checkout gate is DISCOVERED under source_base and its argv is fixed —
        # no verify_command, which load_closeout now rejects outright. See test_verification.py.
        _gate_repo(Path(collab), ok=True)
        closeout = {
            "breaker": "grok",
            "verifier": "gemini",
            "source_base": collab,
            "source_roots": ["src/*.py"],
            "test_path": str(tiny),
        }
        # Autonomous closeout requires a valid v2 catalog (ADR-0005). The example's repo-capable role
        # seats drive the assurance plan; the fake dispatch seats are injected via run(seats=...).
        conftest.write_v2_seats(home, closeout=closeout)

    def test_persistent_lane_defect_escalates_with_repro(self, tmp_path):
        # A defect the lanes CONFIRM on every candidate: each attempt is repair_required, the driver retries
        # to the work-attempt budget, then writes a durable escalation embedding the reproduced defect.
        home = tmp_path / "home"
        home.mkdir()
        collab = str(tmp_path / "c")
        src = Path(collab) / "src" / "gateway.py"
        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            argv = " ".join(cmd)
            who = cmd[0]
            if "builder" in who:
                n["b"] += 1
                src.write_text(f"x = 1  # attempt {n['b']}\n", encoding="utf-8")  # buggy + distinct each time
                return f"attempt {n['b']}"
            if "reviewer" in who:
                return "looks fine\n[[SIGNOFF]]"
            if conftest.is_conformance_prompt(prompt):
                # Requirements met: the persistent LANE defect is what this test is about.
                return conftest.conformance_reply(prompt, source="src/gateway.py:1")
            # The resolved v2 baseline pair speaks the bounded BATCH protocol (ADR-0004 D3); both
            # executors are the claude CLI, so dispatch is told apart by MODEL, not seat name.
            if "gemini-3.5-flash" in argv:  # breaker always finds the persistent defect
                return "FINDING: F1 | src/gateway.py:233 | tz-aware/naive TypeError | gates the snapshot read"
            if "haiku-4.5" in argv:  # verifier confirms it
                return "VERDICT: CONFIRMED F1 | src/gateway.py:233 skew"
            return "ok"

        self._lane_slice(collab, home, tmp_path, runner)
        ap.run(collab, seats=_closeout_seats(), limits=_bounded_limits(2), runner=runner, home=str(home))

        assert escalation.pending(collab) == ["001"]  # escalated
        md = escalation.read(collab, "001")["markdown"]
        assert "gateway.py:233" in md and "PT-3 gateway" in md  # the reproduced defect is embedded
        evs = _events(collab)
        esc = next(e for e in evs if e["stage"] == "autopilot.escalation")
        assert "reason:budget_exhausted" in esc["decision"]["reason_codes"]
        assert hc.state_of(collab, "001") == "claimed"  # not shipped

    def test_reviewer_withhold_escalates_at_budget(self, tmp_path):
        # No confirmed defect — just a reviewer that keeps withholding sign-off (the always-on v2
        # baseline pair reports NO-FINDING below). The candidate is repair_required each attempt and
        # the driver escalates at the work-attempt budget (the ADR-0003 replacement for the old
        # "block without a confirmed defect runs silently to the cap").
        collab, home = str(tmp_path / "c"), str(tmp_path)
        conftest.write_v2_seats(home)  # autonomous closeout requires a v2 catalog (ADR-0005)
        # Typed constraints + a real source file: conformance refuses a handoff that declares no
        # requirements, and refuses evidence whose pointer does not resolve. Either would escalate
        # verification_incomplete and this test would never reach the budget it is about.
        (Path(collab) / "src").mkdir(parents=True, exist_ok=True)
        (Path(collab) / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
        hc.create(
            collab, to="builder", from_="reviewer", title="x", body="y", constraints=[("C1", "exports x")]
        )
        n = {"b": 0}

        def fake(cmd, prompt, **k):
            if "builder" in cmd[0]:
                n["b"] += 1
                return f"rev {n['b']}"  # genuine progress each attempt
            if "reviewer" in cmd[0]:
                return "not yet — keep going"  # reviewer withholds
            if conftest.is_conformance_prompt(prompt):
                return conftest.conformance_reply(prompt, source="src/m.py:1")
            return "NO-FINDING"  # baseline pair clean -> withheld sign-off is the sole blocker

        ap.run(collab, seats=_seats(), max_rounds=2, runner=fake, home=home)
        assert escalation.pending(collab) == ["001"]
        esc = next(e for e in _events(collab) if e["stage"] == "autopilot.escalation")
        assert "reason:budget_exhausted" in esc["decision"]["reason_codes"]
        assert hc.state_of(collab, "001") == "claimed"


class TestOperatorRetry:
    """ADR-0003 reopen=retry: a durable operator request re-drives a PAUSED handoff on a fresh budget
    epoch. The driver consumes it even though the run that escalated it already exited."""

    def test_retry_request_reopens_and_closes(self, tmp_path):
        import operator_requests as opreq

        home = tmp_path / "home"
        home.mkdir()
        collab = str(tmp_path / "c")
        src = Path(collab) / "src" / "gateway.py"
        phase = {"sign": False}
        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            who = cmd[0]
            if "builder" in who:
                n["b"] += 1
                src.write_text(f"x = {n['b']}\n", encoding="utf-8")  # distinct (fixed) source each attempt
                return "built"
            if "reviewer" in who:
                return "verified\n[[SIGNOFF]]" if phase["sign"] else "not yet — keep going"
            if conftest.is_conformance_prompt(prompt):
                return conftest.conformance_reply(prompt, source="src/gateway.py:1")
            if "gemini-3.5-flash" in " ".join(cmd):  # v2 baseline breaker finds nothing -> clean lanes
                return "NO-FINDING"
            return "ok"

        TestLoopPolicy()._lane_slice(collab, home, tmp_path, runner)

        # Phase 1: the reviewer withholds -> repair to the work-attempt budget -> escalation, still claimed.
        ap.run(collab, seats=_closeout_seats(), limits=_bounded_limits(1), runner=runner, home=str(home))
        assert escalation.pending(collab) == ["001"]
        assert hc.state_of(collab, "001") == "claimed"

        # The operator files a durable retry, then the reviewer will sign off; a NEW run consumes it.
        opreq.write(collab, "001", opreq.RETRY, by="operator")
        phase["sign"] = True
        ap.run(collab, seats=_closeout_seats(), limits=_bounded_limits(2), runner=runner, home=str(home))

        assert hc.state_of(collab, "001") == "done"  # retried, re-driven, and closed
        assert opreq.get(collab, "001") is None  # the request was consumed
        assert escalation.pending(collab) == []  # the stale escalation was cleared on reopen
        assert any(e.get("stage") == "autopilot.reopen" for e in _events(collab))

    def test_adopt_request_assesses_current_source_without_a_builder_turn(self, tmp_path):
        import operator_requests as opreq

        home = tmp_path / "home"
        home.mkdir()
        collab = str(tmp_path / "c")
        calls = {"builder": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            who = cmd[0]
            if "builder" in who:
                calls["builder"] += 1
                return "built"
            if "reviewer" in who:
                return "verified\n[[SIGNOFF]]"
            if conftest.is_conformance_prompt(prompt):
                return conftest.conformance_reply(prompt, source="src/gateway.py:1")
            if "gemini-3.5-flash" in " ".join(cmd):  # v2 baseline breaker
                return "NO-FINDING"
            return "ok"

        TestLoopPolicy()._lane_slice(collab, home, tmp_path, runner)
        hc.claim(collab, "001")  # a paused, claimed handoff
        (Path(collab) / "src" / "gateway.py").write_text(
            "x = 1\n", encoding="utf-8"
        )  # operator's known-good source
        opreq.write(collab, "001", opreq.ADOPT, by="operator")

        ap.run(collab, seats=_closeout_seats(), limits=_bounded_limits(2), runner=runner, home=str(home))
        assert hc.state_of(collab, "001") == "done"  # adopted current source, contract satisfied -> done
        assert calls["builder"] == 0  # adopt skipped the builder turn entirely
