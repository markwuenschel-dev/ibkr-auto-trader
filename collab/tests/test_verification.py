"""The done-gate may not call a pytest-only run green.

Regression for 2026-07-15: ``done_contract`` condition 5 read ``tests.passed`` -- produced by a
pytest-only subprocess -- as though it attested the checkout. Lint and type failures were
structurally invisible to the autonomous done-gate.

The property under test is the one the incident violated: **a passing pytest-only run must not be
able to reach the same status as an authoritative verification pass.**
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "lib"))

import done_contract as dcon  # noqa: E402
import verification as v  # noqa: E402

# Reuse the condition-5 harness rather than rebuild it: _setup builds a claimed handoff, _ledger
# writes a ledger that satisfies the other ten conditions so only condition 5 is under test.
from test_done_contract import _ledger, _setup  # noqa: E402


def _repo(tmp_path, *, verify_exit=0, pytest_exit=0):
    """A throwaway git repo whose scripts/verify.py exits with ``verify_exit``."""
    base = tmp_path / "repo"
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "verify.py").write_text(f"import sys; sys.exit({verify_exit})\n")
    (base / "tests").mkdir()
    (base / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert " + ("True" if pytest_exit == 0 else "False") + "\n"
    )
    for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                                 "commit", "-qm", "x"]):
        subprocess.run(["git", *argv], cwd=base, capture_output=True)
    return base


# --------------------------------------------------------------------------- #
# the property: pytest-only can never be green
# --------------------------------------------------------------------------- #


def test_passing_pytest_only_is_not_green(tmp_path):
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert rec["passed"] is True, "precondition: the pytest run itself passes"
    assert rec["exit_code"] == 0
    assert v.is_green(rec) is False, "a passing pytest-only run must NOT be green"
    assert rec["authoritative"] is False
    assert rec["kind"] == v.KIND_PYTEST_ONLY


def test_pytest_only_label_names_what_it_did_not_check(tmp_path):
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert rec["label"] == "PYTEST PASS — lint and types not checked"
    assert "lint" in rec["label"] and "types" in rec["label"]
    for forbidden in ("GREEN", "DONE", "checkout passed"):
        assert forbidden not in rec["label"], f"a partial result must never say {forbidden!r}"


def test_pytest_only_cannot_be_forged_green_by_flipping_fields(tmp_path):
    """is_green demands the whole authoritative shape, not one flag."""
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert v.is_green({**rec, "authoritative": True}) is False
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE}) is False
    # only the full authoritative shape passes
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE, "authoritative": True}) is True


def test_authoritative_pass_is_green(tmp_path):
    base = _repo(tmp_path, verify_exit=0)
    rec = v.run_authoritative(base)
    if rec["exit_code"] != 0:
        pytest.skip(f"uv unavailable in this environment: {rec['label']}")
    assert v.is_green(rec) is True
    assert rec["label"] == "GREEN — scripts/verify.py exit 0"
    assert rec["kind"] == v.KIND_AUTHORITATIVE


def test_authoritative_failure_is_not_green(tmp_path):
    base = _repo(tmp_path, verify_exit=1)
    rec = v.run_authoritative(base)
    assert v.is_green(rec) is False
    assert "GREEN" not in rec["label"] or rec["label"].startswith("FAIL")


def test_unverified_is_not_green():
    assert v.is_green(v.unverified("no test_path")) is False
    assert v.is_green({}) is False
    assert v.is_green(None) is False


# --------------------------------------------------------------------------- #
# checkout-state guard
# --------------------------------------------------------------------------- #


def test_exit_zero_over_a_moved_checkout_is_void(tmp_path):
    """A builder mutating the tree mid-verify invalidates the result -- the 2026-07-15 shape."""
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    moved = {**rec, "kind": v.KIND_AUTHORITATIVE, "authoritative": True,
             "end_sha": "deadbeef", "checkout_stable": False}
    assert v.is_green(moved) is False, "exit 0 attests the tree it ran on; that tree is gone"


def test_the_verifiers_own_cache_exhaust_does_not_void_the_run(tmp_path):
    """pytest writes __pycache__; that is not the checkout moving.

    Caught by this suite: the first cut of the guard compared raw ``git status`` and voided every
    single run, because running pytest dirties the tree with its own caches.
    """
    base = _repo(tmp_path)  # deliberately has NO .gitignore
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert (base / "tests" / "__pycache__").exists(), "precondition: pytest wrote its cache"
    assert rec["checkout_stable"] is True, f"cache exhaust voided the run: {rec['end_status']!r}"
    assert rec["label"] == v.LABEL_PYTEST_ONLY
    assert rec["label"] != v.LABEL_CHECKOUT_MOVED


def test_a_real_source_edit_during_the_run_does_void_it(tmp_path):
    """The guard must still catch what it exists for -- an actual source change mid-run."""
    base = _repo(tmp_path)
    before = v._checkout_state(base)
    (base / "tests" / "test_x.py").write_text("def test_x():\n    assert True  # edited\n")
    after = v._checkout_state(base)
    assert before["status"] != after["status"], "a tracked edit must register as the tree moving"


def test_record_carries_the_full_provenance(tmp_path):
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    for field in ("command", "exit_code", "started_ts", "ended_ts", "start_sha", "end_sha",
                  "start_status", "end_status", "checkout_stable", "label", "kind"):
        assert field in rec, f"ledger record must record {field}"
    assert rec["start_sha"] and len(rec["start_sha"]) == 40
    assert rec["start_sha"] == rec["end_sha"]
    assert rec["checkout_stable"] is True
    assert rec["command"][1:3] == ["-m", "pytest"]


def test_authoritative_argv_is_unflagged():
    """Any narrowing flag makes verify.py partial -- see its module docstring."""
    assert v.AUTHORITATIVE_ARGV == ("uv", "run", "--locked", "python", "scripts/verify.py")
    for flag in ("--python-only", "--no-build", "--fail-fast"):
        assert flag not in v.AUTHORITATIVE_ARGV


# --------------------------------------------------------------------------- #
# done_contract condition 5 -- the actual consumer
# --------------------------------------------------------------------------- #


def _green_record(base=None):
    """The only shape that may close a handoff: authoritative, exit 0, checkout did not move.

    SHA/status are read from ``base`` when given, because condition 5 also pins the receipt to the
    checkout on disk (:func:`verification.matches_checkout`) — a hardcoded SHA is, correctly, a
    different-checkout receipt and is refused.
    """
    state = v._checkout_state(base) if base is not None else {"sha": "a" * 40, "status": ""}
    return {"kind": v.KIND_AUTHORITATIVE, "authoritative": True, "exit_code": 0, "passed": True,
            "checkout_stable": True, "label": v.LABEL_GREEN, "command": list(v.AUTHORITATIVE_ARGV),
            "start_sha": state["sha"], "end_sha": state["sha"],
            "start_status": state["status"], "end_status": state["status"],
            "started_ts": "2026-07-15T11:19:18Z", "ended_ts": "2026-07-15T11:20:18Z",
            "run_id": "authoritative-1"}


def _cond(collab, cid=5):
    ev = dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer", builder_seat="builder")
    return next(c for c in ev["conditions"] if c["id"] == cid)


def test_condition5_rejects_a_passing_pytest_only_ledger(tmp_path):
    """The incident, as a test: a green-looking pytest-only ledger must not satisfy condition 5."""
    collab = _setup(tmp_path)
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert rec["passed"] is True, "precondition: the pytest run itself passes"
    _ledger(collab, tests=rec)
    c5 = _cond(collab)
    assert c5["status"] != "pass", "condition 5 must refuse a pytest-only result"
    assert "lint and types not checked" in c5["detail"]


def test_condition5_rejects_the_legacy_bare_passed_boolean(tmp_path):
    """The exact pre-2026-07-15 ledger shape must no longer close a handoff.

    ``{"passed": true, "run_id": "pytest-..."}`` is what ledgers 030/031/034 carried on 2026-07-15.
    """
    collab = _setup(tmp_path)
    _ledger(collab, tests={"passed": True, "run_id": "pytest-1784114098557517700"})
    assert _cond(collab)["status"] != "pass", "a bare {passed: true} must not satisfy the contract"


def test_condition5_accepts_an_authoritative_green_ledger(tmp_path):
    collab = _setup(tmp_path)
    _ledger(collab, tests=_green_record(collab))
    assert _cond(collab)["status"] == "pass"


# --------------------------------------------------------------------------- #
# fail-closed: absent / malformed / stale / nonzero / mismatched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("absent", lambda r: {}),
        ("none", lambda r: None),
        ("malformed-string", lambda r: "GREEN"),
        ("malformed-list", lambda r: ["passed"]),
        ("nonzero-exit", lambda r: {**r, "exit_code": 1, "passed": False}),
        ("exit-nonzero-but-passed-true", lambda r: {**r, "exit_code": 1}),
        ("checkout-moved", lambda r: {**r, "checkout_stable": False}),
        ("kind-stripped", lambda r: {**r, "kind": None}),
        ("authoritative-stripped", lambda r: {**r, "authoritative": False}),
        ("passed-none", lambda r: {**r, "passed": None}),
        ("missing-exit-code", lambda r: {k: v for k, v in r.items() if k != "exit_code"}),
    ],
)
def test_is_green_fails_closed(name, mutate):
    """Every degraded record shape must be refused. Fail-closed, not fail-open."""
    assert v.is_green(mutate(_green_record())) is False, f"{name} must not be green"


def test_condition5_fails_closed_on_a_missing_tests_key(tmp_path):
    collab = _setup(tmp_path)
    _ledger(collab, tests=None)
    assert _cond(collab)["status"] != "pass"


def test_a_receipt_from_a_different_checkout_is_refused(tmp_path):
    """A green receipt earned on an earlier commit must not close later work it never examined."""
    base = _repo(tmp_path)
    rec = v.run_authoritative(base, [sys.executable, "-c", "import sys; sys.exit(0)"])
    assert v.is_green(rec) is True, "precondition: a genuine green receipt for this checkout"
    ok, why = v.matches_checkout(rec, base)
    assert ok is True, why

    # a new commit: same receipt, different checkout
    (base / "tests" / "test_y.py").write_text("def test_y():\n    assert True\n")
    for argv in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "y"]):
        subprocess.run(["git", *argv], cwd=base, capture_output=True)

    assert v.is_green(rec) is True, "the record is still internally coherent..."
    ok, why = v.matches_checkout(rec, base)
    assert ok is False, "...but it attests a checkout that is no longer on disk"
    assert "different checkout" in why


def test_a_dirty_tree_after_the_receipt_is_refused(tmp_path):
    """An uncommitted source edit after the receipt invalidates it (no new commit required)."""
    base = _repo(tmp_path)
    rec = v.run_authoritative(base, [sys.executable, "-c", "import sys; sys.exit(0)"])
    assert v.matches_checkout(rec, base)[0] is True
    (base / "tests" / "test_x.py").write_text("def test_x():\n    assert True  # edited\n")
    ok, why = v.matches_checkout(rec, base)
    assert ok is False and "working tree changed" in why


def test_condition5_refuses_a_stale_receipt_end_to_end(tmp_path):
    """done_contract must refuse a green record whose SHA is not the checkout being closed."""
    collab = _setup(tmp_path)
    _ledger(collab, tests={**_green_record(collab), "end_sha": "b" * 40})
    c5 = _cond(collab)
    assert c5["status"] != "pass"
    assert "different checkout" in c5["detail"]


def test_a_pytest_only_ledger_cannot_close_the_whole_contract(tmp_path):
    """End to end: satisfied must be False on pytest-only evidence, and True on authoritative."""
    collab = _setup(tmp_path)
    base = _repo(tmp_path)
    _ledger(collab, tests=v.run_pytest_only("tests", str(base), python=sys.executable))
    assert dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer",
                         builder_seat="builder")["satisfied"] is False
    _ledger(collab, tests=_green_record(collab))
    assert dcon.evaluate(collab, "001", seats={}, reviewer_seat="reviewer",
                         builder_seat="builder")["satisfied"] is True


def test_pytest_only_and_authoritative_never_share_a_status(tmp_path):
    """The property, stated directly: both say passed=True; nothing downstream may treat them alike."""
    collab = _setup(tmp_path)
    base = _repo(tmp_path)
    partial = v.run_pytest_only("tests", str(base), python=sys.executable)
    green = _green_record(collab)
    assert partial["passed"] == green["passed"] is True, "both 'passed' -- that was the trap"
    assert v.is_green(partial) != v.is_green(green)
    assert partial["label"] != green["label"]

    _ledger(collab, tests=partial)
    partial_status = _cond(collab)["status"]
    _ledger(collab, tests=green)
    green_status = _cond(collab)["status"]
    assert partial_status != green_status
