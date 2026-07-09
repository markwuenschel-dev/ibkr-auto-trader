"""Tests for done_contract.py — the Autonomous Done-Transition Contract evaluator (§18.3).

evaluate() is pure: it reads the verification ledger + live source + handoff state and returns a verdict,
never transitioning anything. These tests pin each of the ten conditions independently.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import done_contract as dcon  # noqa: E402
import gate_runner as gr  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402


def _setup(tmp_path):
    collab = str(tmp_path / "c")
    hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
    hc.claim(collab, "001")  # -> claimed (condition 10 wants pending|claimed)
    return collab


def _preflight(base, *, seat="reviewer", **over):
    """A valid reviewer repo-preflight block (condition 11) — hand-built so no real git is needed here."""
    pre = {"seat": seat, "repo_access": True, "repo_root": str(base),
           "commands": {"pwd": {"exit_code": 0, "stdout_tail": str(base)},
                        "git_rev_parse": {"exit_code": 0, "stdout_tail": str(base)},
                        "git_status_short": {"exit_code": 0, "stdout_tail": ""},
                        "git_diff_name_only": {"exit_code": 0, "stdout_tail": ""},
                        "pytest_collect_only": {"exit_code": 0, "stdout_tail": "1 test collected"}},
           "inspected_files": ["src/m.py"]}
    pre.update(over)
    return pre


def _ledger(collab, hid="001", **over):
    base = Path(collab)
    (base / "src").mkdir(parents=True, exist_ok=True)
    (base / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    led = {"hid": hid, "generated_ts": ap._now_utc(), "guardrails": [],
           "builder_seat": "builder", "reviewer_seat": "reviewer",
           "source_base": str(base), "source_manifest": gr.source_manifest(["src/*.py"], base),
           "tests": {"passed": True, "run_id": "t"}, "reviewer_preflight": _preflight(base),
           "lanes": [], "blockers": [], "accepted_residuals": []}
    led.update(over)
    lanes.write_ledger(collab, hid, led)
    return led


def _failed(v):
    return {c["name"] for c in v["conditions"] if c["status"] != "pass"}


def _eval(collab):
    return dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer", builder_seat="builder")


class TestEvaluate:
    def test_all_eleven_conditions_satisfied(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab)
        v = _eval(collab)
        assert v["satisfied"] is True
        assert len(v["conditions"]) == 11 and _failed(v) == set()

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
        _ledger(collab, guardrails=["bounded-autonomy", "untrusted-agent-output"], lanes=[])  # 5 required, 0 ran
        assert "lanes-ran" in _failed(_eval(collab))

    def test_unfixed_blocker_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, blockers=[{"id": "b1", "lane": "x", "description": "d",
                                   "fixed": False, "regression_test": "t"}])
        assert "blockers-fixed" in _failed(_eval(collab))

    def test_blocker_without_regression_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, blockers=[{"id": "b1", "lane": "x", "description": "d",
                                   "fixed": True, "regression_test": None}])
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
        _ledger(collab, source_base=str(scratch),
                source_manifest=gr.source_manifest(["src/*.py"], scratch))
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
        _ledger(collab, reviewer_preflight=_preflight(Path(collab),
                                                      inspected_files=["../../../etc/passwd"]))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))

    def test_absolute_inspected_path_fails(self, tmp_path):
        collab = _setup(tmp_path)
        _ledger(collab, reviewer_preflight=_preflight(Path(collab),
                                                      inspected_files=[str(Path(collab) / "src" / "m.py")]))
        assert "reviewer-repo-preflight" in _failed(_eval(collab))
