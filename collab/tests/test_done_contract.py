"""Tests for done_contract.py — the Autonomous Done-Transition Contract evaluator (§18.3).

evaluate() is pure: it reads the verification ledger + live source + handoff state and returns a verdict,
never transitioning anything. These tests pin each of the ten conditions independently.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import done_contract as dcon  # noqa: E402
import gate_runner as gr  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import verification as _vfy  # noqa: E402


def git_checkout(base) -> None:
    """Make ``base`` a real git checkout with a STABLE ``git status``.

    Condition 5 now requires a resolvable repo root and a non-null HEAD (``None == None`` stopped
    counting as a SHA match), and it pins the receipt to the tree's status. These tests keep writing
    into the same directory they treat as the source under review — the ledger itself lands there — so
    ignoring everything but ``.gitignore`` keeps the porcelain status empty and the receipt pinned no
    matter what a test writes next. Idempotent: re-running init/commit on an existing repo is a no-op.
    """
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    (base / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    for argv in (
        ["init", "-q"],
        ["add", "-f", ".gitignore"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t"],
    ):
        subprocess.run(["git", *argv], cwd=str(base), capture_output=True)


def green_record(base) -> dict:
    """The ONLY record shape condition 5 accepts: canonical argv, exit 0, bound to ``base``'s checkout."""
    st = _vfy._checkout_state(base)
    return {
        "kind": _vfy.KIND_AUTHORITATIVE,
        "authoritative": True,
        "canonical_command": True,
        "exit_code": 0,
        "passed": True,
        "checkout_stable": True,
        "label": _vfy.LABEL_GREEN,
        "command": list(_vfy.AUTHORITATIVE_ARGV),
        "repo_root": st["root"],
        "start_sha": st["sha"],
        "end_sha": st["sha"],
        "start_status": st["status"],
        "end_status": st["status"],
        "started_ts": "2026-07-15T11:19:18Z",
        "ended_ts": "2026-07-15T11:20:18Z",
        "run_id": "t",
    }


def _setup(tmp_path):
    collab = str(tmp_path / "c")
    hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
    hc.claim(collab, "001")  # -> claimed (condition 10 wants pending|claimed)
    git_checkout(collab)
    return collab


def _preflight(base, *, seat="reviewer", **over):
    """A valid reviewer repo-preflight block (condition 11) — hand-built so no real git is needed here."""
    pre = {
        "seat": seat,
        "repo_access": True,
        "repo_root": str(base),
        "commands": {
            "pwd": {"exit_code": 0, "stdout_tail": str(base)},
            "git_rev_parse": {"exit_code": 0, "stdout_tail": str(base)},
            "git_status_short": {"exit_code": 0, "stdout_tail": ""},
            "git_diff_name_only": {"exit_code": 0, "stdout_tail": ""},
            "pytest_collect_only": {"exit_code": 0, "stdout_tail": "1 test collected"},
        },
        "inspected_files": ["src/m.py"],
    }
    pre.update(over)
    return pre


def _ledger(collab, hid="001", **over):
    base = Path(collab)
    (base / "src").mkdir(parents=True, exist_ok=True)
    (base / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    git_checkout(base)
    led = {
        "hid": hid,
        "generated_ts": ap._now_utc(),
        "guardrails": [],
        "builder_seat": "builder",
        "reviewer_seat": "reviewer",
        "source_base": str(base),
        "source_manifest": gr.source_manifest(["src/*.py"], base),
        # An AUTHORITATIVE green record: canonical argv, exit 0, bound to the checkout under review.
        # Condition 5 refuses a pytest-only result (the 2026-07-15 regression), a non-canonical command,
        # a receipt from another checkout, and a tree with no git identity at all. Each is pinned in
        # collab/tests/test_verification.py.
        "tests": green_record(base),
        "reviewer_preflight": _preflight(base),
        # A v2 ledger owns its RESOLVED plan (ADR-0004 D4/ADR-0005). Condition 3 requires the plan to be
        # present — without it, required passes fall back to mutable current config, which is how a
        # legacy fan-out candidate (no seat validation) could satisfy the condition vacuously.
        "verification_plan": {"passes": [{"id": "baseline"}]},
        "verification_plan_digest": "plan:test-digest",
        "lanes": [{"pass": "baseline", "ran": True, "confirmed": [], "refuted": []}],
        # A satisfied, candidate-bound conformance record (ADR-0005, condition 12). Conditions 1..11
        # gate mechanical evidence and cannot see a requirement the change simply omits.
        "conformance": {
            "candidate_id": None,
            "contract_digest": "conformance:test-digest",
            "requirement_ids": ["C1"],
            "results": [{"id": "C1", "status": "met"}],
            "incomplete": None,
            "satisfied": True,
        },
        "blockers": [],
        "accepted_residuals": [],
    }
    led.update(over)
    lanes.write_ledger(collab, hid, led)
    return led


def _failed(v):
    return {c["name"] for c in v["conditions"] if c["status"] != "pass"}


def _eval(collab):
    return dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer", builder_seat="builder")


class TestEvaluate:
    def test_all_twelve_conditions_satisfied(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab)
        v = _eval(collab)
        assert v["satisfied"] is True
        assert len(v["conditions"]) == 12 and _failed(v) == set()

    def test_a_ledger_with_no_conformance_record_cannot_close(self, tmp_path):
        # The 2026-07-16 shape: every mechanical condition green, and a requirement silently omitted.
        # Absence of the record is refusal, never "nothing to check".
        collab = _setup(tmp_path)
        _ledger(collab, conformance=None)
        v = _eval(collab)
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_an_unmet_requirement_cannot_close(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab,
            conformance={
                "candidate_id": None,
                "contract_digest": "conformance:d",
                "requirement_ids": ["C1", "C2"],
                "results": [{"id": "C1", "status": "met"}, {"id": "C2", "status": "missing"}],
                "incomplete": None,
            },
        )
        v = _eval(collab)
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_incomplete_conformance_is_not_a_pass(self, tmp_path):
        # Disagreement/malformed/unresolvable evidence is UNKNOWN. Unknown must never close.
        collab = _setup(tmp_path)
        _ledger(
            collab,
            conformance={
                "candidate_id": None,
                "contract_digest": "conformance:d",
                "requirement_ids": ["C1"],
                "results": [],
                "incomplete": {"reason": "disagreement", "detail": "assessor=met verifier=missing"},
            },
        )
        v = _eval(collab)
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_a_contract_declaring_no_requirements_cannot_close_vacuously(self, tmp_path):
        # The condition-3 lesson: a check that requires nothing passes for free.
        collab = _setup(tmp_path)
        _ledger(
            collab,
            conformance={
                "candidate_id": None,
                "contract_digest": "conformance:d",
                "requirement_ids": [],
                "results": [],
                "incomplete": None,
            },
        )
        v = _eval(collab)
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_conformance_evidence_bound_to_another_candidate_is_refused(self, tmp_path):
        # An unbound record could be replayed from a different candidate.
        collab = _setup(tmp_path)
        _ledger(
            collab,
            conformance={
                "candidate_id": "cand:someone-else",
                "contract_digest": "conformance:d",
                "requirement_ids": ["C1"],
                "results": [{"id": "C1", "status": "met"}],
                "incomplete": None,
            },
        )
        v = dcon.evaluate(
            collab,
            "001",
            seats={},
            reviewer_seat="reviewer",
            builder_seat="builder",
            candidate_id="cand:this-one",
        )
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_coverage_mismatch_is_refused(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab,
            conformance={
                "candidate_id": None,
                "contract_digest": "conformance:d",
                "requirement_ids": ["C1", "C2"],
                "results": [{"id": "C1", "status": "met"}],  # C2 never adjudicated
                "incomplete": None,
            },
        )
        v = _eval(collab)
        assert v["satisfied"] is False and "spec-conformance" in _failed(v)

    def test_ledger_without_a_resolved_plan_cannot_close(self, tmp_path):
        # ADR-0005: a legacy fan-out ledger carries no plan, so `ledger_required_passes` would fall back
        # to MUTABLE current config — with no guardrails that is ZERO required passes, and condition 3
        # passed vacuously. That is the path on which a text-only adapter could sit in a verifier seat.
        # Every other condition here is green; the missing plan alone must refuse the close.
        collab = _setup(tmp_path)
        _ledger(collab, verification_plan=None, verification_plan_digest="", lanes=[])
        v = _eval(collab)
        assert v["satisfied"] is False
        assert "lanes-ran" in _failed(v)

    def test_plan_present_but_digest_missing_cannot_close(self, tmp_path):
        # The plan must be BOUND (digest), not merely echoed: an unbound plan is unattributable evidence.
        collab = _setup(tmp_path)
        _ledger(collab, verification_plan_digest="")
        v = _eval(collab)
        assert v["satisfied"] is False
        assert "lanes-ran" in _failed(v)

    def test_no_ledger_fails_evidence_conditions(self, tmp_path):
        collab = _setup(tmp_path)
        v = _eval(collab)
        assert v["satisfied"] is False
        assert {"builder-evidence", "source==tested", "lanes-ran"} <= _failed(v)

    def test_reviewer_equals_builder_fails_independence(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, builder_seat="reviewer")
        v = dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer", builder_seat="reviewer")
        assert "independent-approver" in _failed(v) and v["satisfied"] is False

    def test_missing_required_lane_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab, guardrails=["bounded-autonomy", "untrusted-agent-output"], lanes=[]
        )  # 5 required, 0 ran
        assert "lanes-ran" in _failed(_eval(collab))

    def test_unfixed_blocker_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab,
            blockers=[{"id": "b1", "lane": "x", "description": "d", "fixed": False, "regression_test": "t"}],
        )
        assert "blockers-fixed" in _failed(_eval(collab))

    def test_blocker_without_regression_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab,
            blockers=[{"id": "b1", "lane": "x", "description": "d", "fixed": True, "regression_test": None}],
        )
        assert "blocker-regressions" in _failed(_eval(collab))

    def test_tests_not_passed_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, tests={"passed": False, "run_id": "t"})
        assert "blocker-regressions" in _failed(_eval(collab))

    def test_source_drift_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab)
        (Path(collab) / "src" / "m.py").write_text("x = 2\n", encoding="utf-8")  # drift after manifest
        assert "source==tested" in _failed(_eval(collab))

    def test_scratchpad_evidence_fails(self, tmp_path):
        collab = _setup(tmp_path)
        scratch = tmp_path / "scratchpad" / "s"
        (scratch / "src").mkdir(parents=True)
        (scratch / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
        _ledger(collab, source_base=str(scratch), source_manifest=gr.source_manifest(["src/*.py"], scratch))
        assert "no-stale-evidence" in _failed(_eval(collab))

    def test_verdict_hash_is_stable(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab)
        assert _eval(collab)["hash"] == _eval(collab)["hash"]


class TestReviewerPreflight:
    """Condition 11 — the signing reviewer must prove repo awareness (repo-aware preflight)."""

    def test_missing_preflight_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=None)
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_wrong_seat_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=_preflight(Path(collab), seat="someone-else"))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_no_repo_access_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=_preflight(Path(collab), repo_access=False))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_git_rev_parse_failed_fails(self, tmp_path):
        collab = _setup(tmp_path)
        pre = _preflight(Path(collab))
        pre["commands"]["git_rev_parse"] = {"exit_code": 128, "stdout_tail": "not a git repo"}
        _ledger(collab, reviewer_preflight=pre)
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_collect_only_failed_fails(self, tmp_path):
        collab = _setup(tmp_path)
        pre = _preflight(Path(collab))
        pre["commands"]["pytest_collect_only"] = {"exit_code": 5, "stdout_tail": "no tests ran"}
        _ledger(collab, reviewer_preflight=pre)
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_no_inspected_files_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=_preflight(Path(collab), inspected_files=[]))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_escaping_inspected_path_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=_preflight(Path(collab), inspected_files=["../../../etc/passwd"]))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_absolute_inspected_path_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(
            collab,
            reviewer_preflight=_preflight(Path(collab), inspected_files=[str(Path(collab) / "src" / "m.py")]),
        )
        assert "reviewer-repo-preflight" in _failed(_eval(collab))
