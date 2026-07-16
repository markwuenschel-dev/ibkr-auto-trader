"""Only the canonical command, over a real git checkout, may call a checkout green.

Regression for 2026-07-15: ``done_contract`` condition 5 read ``tests.passed`` -- produced by a
pytest-only subprocess -- as though it attested the checkout. Lint and type failures were
structurally invisible to the autonomous done-gate.

Regression for the 2026-07-15 AUDIT of that fix, which closed the pytest-only hole and left two more:

  1. ``run_authoritative(base, argv)`` ran ANY operator-supplied argv and stamped it
     ``kind="authoritative"``. ``python -c "print('RESULT: PASS')"`` earned the label
     "GREEN — scripts/verify.py exit 0". The command's identity was never checked.
  2. ``checkout_stable`` was ``start_sha == end_sha``, which is TRUE for ``None == None`` -- so a
     directory with no git at all satisfied both the stability guard and ``matches_checkout``.

The properties under test:

  * a passing pytest-only run must not reach the same status as an authoritative pass;
  * ONLY :data:`verification.AUTHORITATIVE_ARGV` may produce an authoritative record, and it is
    refused BEFORE execution otherwise -- output text like ``RESULT: PASS`` proves nothing;
  * autonomous closure requires a real git checkout that held still.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "lib"))

import collab_common as cc
import done_contract as dcon
import verification as v

# Reuse the condition-5 harness rather than rebuild it: _setup builds a claimed handoff, _ledger
# writes a ledger that satisfies the other ten conditions so only condition 5 is under test.
from test_done_contract import _ledger, _setup, green_record


def _repo(tmp_path, *, verify_exit=0, pytest_exit=0, uv_project=False):
    """A throwaway git repo whose scripts/verify.py exits with ``verify_exit``.

    ``uv_project=True`` also makes it a zero-dependency uv project, so the CANONICAL command
    (``uv run --locked python scripts/verify.py``) genuinely runs here — the only way to test the
    authoritative path now that no stand-in argv is accepted. Off by default: ``uv lock`` costs ~2s
    (offline, no network) and most tests here never invoke the gate.
    """
    base = tmp_path / "repo"
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "verify.py").write_text(f"import sys; sys.exit({verify_exit})\n")
    (base / "tests").mkdir()
    (base / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert " + ("True" if pytest_exit == 0 else "False") + "\n"
    )
    if uv_project:
        (base / "pyproject.toml").write_text(
            '[project]\nname = "t"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n'
        )
        subprocess.run(["uv", "lock"], cwd=base, capture_output=True)
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
    """is_green demands the whole authoritative shape, not one flag.

    Flipping ``kind`` + ``authoritative`` on a pytest-only record USED to be enough — the command was
    never re-checked, so a record could claim to be something its own argv contradicted. It now takes
    the canonical argv too, and ``is_green`` re-derives that from ``command`` rather than trusting the
    ``canonical_command`` flag, because the record is ledger JSON a builder could have written.
    """
    base = _repo(tmp_path)
    rec = v.run_pytest_only("tests", str(base), python=sys.executable)
    assert v.is_green({**rec, "authoritative": True}) is False
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE}) is False
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE, "authoritative": True}) is False
    # even asserting the flag does not help: the argv is still pytest's
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE, "authoritative": True,
                       "canonical_command": True}) is False
    # only the full authoritative shape, canonical argv included, passes
    assert v.is_green({**rec, "kind": v.KIND_AUTHORITATIVE, "authoritative": True,
                       "command": list(v.AUTHORITATIVE_ARGV)}) is True


def test_authoritative_pass_is_green(tmp_path):
    """THE POSITIVE CASE: the exact canonical command, really executed, over a real checkout."""
    base = _repo(tmp_path, verify_exit=0, uv_project=True)
    rec = v.run_authoritative(base)
    if rec["exit_code"] != 0:
        pytest.skip(f"uv unavailable in this environment: {rec['label']}")
    assert v.is_green(rec) is True
    assert rec["label"] == "GREEN — scripts/verify.py exit 0"
    assert rec["kind"] == v.KIND_AUTHORITATIVE
    assert rec["command"] == list(v.AUTHORITATIVE_ARGV)
    assert rec["canonical_command"] is True
    assert v.is_bound_to_git(rec)[0] is True


def test_authoritative_failure_is_not_green(tmp_path):
    base = _repo(tmp_path, verify_exit=1, uv_project=True)
    rec = v.run_authoritative(base)
    assert v.is_green(rec) is False
    assert "GREEN" not in rec["label"] or rec["label"].startswith("FAIL")


def test_unverified_is_not_green():
    assert v.is_green(v.unverified("no test_path")) is False
    assert v.is_green({}) is False
    assert v.is_green(None) is False


# --------------------------------------------------------------------------- #
# COMMAND IDENTITY — only the canonical argv is authoritative, and it is checked
# BEFORE execution. Regression for the audit finding: run_authoritative ran any
# argv and stamped it authoritative.
# --------------------------------------------------------------------------- #

_NON_CANONICAL = [
    # (name, argv) — every one of these was accepted as AUTHORITATIVE before the audit.
    ("bare-pytest", ["pytest"]),
    ("bare-pytest-via-python", [sys.executable, "-m", "pytest"]),
    ("narrowed-verify", ["uv", "run", "--locked", "python", "scripts/verify.py", "--python-only"]),
    ("narrowed-verify-fail-fast", ["uv", "run", "--locked", "python", "scripts/verify.py", "--fail-fast"]),
    ("wrapper-script", ["bash", "verify-wrapper.sh"]),
    ("prints-the-pass-marker", [sys.executable, "-c", "print('RESULT: PASS (full local matrix is green)')"]),
    ("exits-zero-silently", [sys.executable, "-c", "import sys; sys.exit(0)"]),
    ("extra-argument", ["uv", "run", "--locked", "python", "scripts/verify.py", "-q"]),
    ("missing-argument", ["uv", "run", "--locked", "python"]),
    ("reordered", ["uv", "run", "python", "--locked", "scripts/verify.py"]),
    ("different-script", ["uv", "run", "--locked", "python", "scripts/other.py"]),
    ("shell-string", "uv run --locked python scripts/verify.py"),
    ("empty", []),
    # An EXPLICIT None — what `cfg.get("verify_command")` yields when the key is absent. It must be
    # refused, not silently upgraded to the canonical run: "no command configured" is not "run the
    # default". Omitting the argument entirely still means canonical (test_authoritative_pass_is_green).
    ("missing-verify-command", None),
    ("malformed-nested", ["uv", ["run"], "--locked"]),
    ("malformed-int", ["uv", 3, "--locked"]),
    ("malformed-empty-token", ["uv", "", "--locked", "python", "scripts/verify.py"]),
]


@pytest.mark.parametrize(("name", "argv"), _NON_CANONICAL, ids=[n for n, _ in _NON_CANONICAL])
def test_a_non_canonical_command_is_never_authoritative(name, argv, tmp_path):
    """The whole table must be rejected, and rejected WITHOUT being run."""
    base = _repo(tmp_path)
    rec = v.run_authoritative(base, argv)
    assert v.is_green(rec) is False, f"{name} must not be green"
    assert rec["kind"] != v.KIND_AUTHORITATIVE, f"{name} must not be kind=authoritative"
    assert rec["authoritative"] is False
    assert rec["canonical_command"] is False
    assert rec["exit_code"] is None, f"{name} must be refused BEFORE execution, not judged after"
    assert "GREEN" not in rec["label"]
    assert v.is_canonical_command(argv) is False


def test_seats_json_may_not_configure_the_gate(tmp_path):
    """Fail-closed at CONFIGURATION LOAD: closeout.verify_command is refused, not ignored.

    It used to name the "authoritative whole-checkout gate" and was handed straight to subprocess. A
    silently-dropped key would be worse than a raise: the operator would believe their gate was
    configured and running when it was neither.
    """
    import autopilot as ap

    home = tmp_path / "home"
    home.mkdir()

    def _write(closeout):
        (home / "seats.json").write_text(json.dumps({"version": 1, "closeout": closeout}), "utf-8")

    for bad in (
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        ["pytest"],
        list(v.AUTHORITATIVE_ARGV),  # even the canonical spelling: the gate is not configurable
        None,
    ):
        _write({"breaker": "b", "verifier": "v", "verify_command": bad})
        with pytest.raises(cc.CollabError, match="verify_command"):
            ap.load_closeout(home)

    _write({"breaker": "b", "verifier": "v", "test_path": "tests"})  # without the key: loads fine
    assert ap.load_closeout(home)["breaker"] == "b"


def test_the_canonical_command_is_accepted():
    assert v.is_canonical_command(["uv", "run", "--locked", "python", "scripts/verify.py"]) is True
    assert v.is_canonical_command(list(v.AUTHORITATIVE_ARGV)) is True
    assert v.is_canonical_command(v.AUTHORITATIVE_ARGV) is True


def test_canonical_normalisation_is_narrow():
    """Only whitespace and the Windows path separator are forgiven — never meaning."""
    assert v.is_canonical_command([" uv ", "run", "--locked", "python", "scripts/verify.py"]) is True
    assert v.is_canonical_command(["uv", "run", "--locked", "python", r"scripts\verify.py"]) is True
    # case is meaning: a different path, and no case folding is applied
    assert v.is_canonical_command(["UV", "run", "--locked", "python", "scripts/verify.py"]) is False


def test_output_text_cannot_earn_green(tmp_path):
    """A command printing the PASS marker is still not the gate. Identity, not output."""
    base = _repo(tmp_path)
    liar = [sys.executable, "-c", "print('RESULT: PASS (full local matrix is green)')"]
    rec = v.run_authoritative(base, liar)
    assert v.is_green(rec) is False
    assert rec["exit_code"] is None, "it must never even run"


def test_a_missing_verify_command_falls_back_to_unverified_not_green():
    """No command configured is UNVERIFIED — never a pass. Fail-closed, not fail-open."""
    for record in (v.unverified("no verify_command"), v.unverified(""), {}):
        assert v.is_green(record) is False
    assert v.is_green(v.run_pytest_only(None, ".", python=sys.executable)) is False


def test_a_ledger_record_claiming_canonical_but_carrying_another_argv_is_refused():
    """is_green re-derives canonicality from `command`; the flag in the JSON is not evidence."""
    forged = {**_green_record(), "command": ["pytest"], "canonical_command": True}
    assert v.is_green(forged) is False


# --------------------------------------------------------------------------- #
# GIT BINDING — a receipt with no git identity attests nothing. Regression for
# the audit finding: None == None counted as a SHA match.
# --------------------------------------------------------------------------- #


def test_a_non_git_tree_cannot_close_autonomously(tmp_path):
    """The headline: a directory with no git at all must fail, with an explicit reason."""
    base = tmp_path / "plain"
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "verify.py").write_text("import sys; sys.exit(0)\n")

    state = v._checkout_state(base)
    assert state["sha"] is None and state["root"] is None, "precondition: not a git checkout"

    rec = v.run_authoritative(base, list(v.AUTHORITATIVE_ARGV))
    assert rec["start_sha"] is None and rec["end_sha"] is None
    assert rec["checkout_stable"] is False, "None == None is not a SHA match"
    assert v.is_green(rec) is False

    bound, why = v.is_bound_to_git(rec)
    assert bound is False
    assert "git" in why.lower(), why

    ok, why2 = v.matches_checkout(rec, base)
    assert ok is False, "a receipt with no identity cannot be pinned to anything"
    assert "not a git checkout" in why2


def test_none_shas_are_not_a_match_at_any_layer():
    null_id = {**_green_record(), "repo_root": None, "start_sha": None, "end_sha": None,
               "checkout_stable": True}
    assert v.is_green(null_id) is False
    assert v.is_bound_to_git(null_id)[0] is False


def test_a_receipt_from_another_repository_root_is_refused(tmp_path):
    """Same SHA, different repo (a fork/mirror/submodule) attests someone else's tree."""
    base = _repo(tmp_path)
    rec = _green_record(base)
    assert v.matches_checkout(rec, base)[0] is True
    elsewhere = {**rec, "repo_root": str(tmp_path / "some-other-repo")}
    ok, why = v.matches_checkout(elsewhere, base)
    assert ok is False
    assert "different repository root" in why


def test_a_sha_change_during_verification_voids_the_run():
    moved = {**_green_record(), "end_sha": "b" * 40, "checkout_stable": False}
    assert v.is_green(moved) is False
    bound, why = v.is_bound_to_git(moved)
    assert bound is False and "moved" in why.lower()


def test_a_status_change_during_verification_voids_the_run():
    dirtied = {**_green_record(), "end_status": " M src/x.py", "checkout_stable": False}
    assert v.is_green(dirtied) is False
    bound, why = v.is_bound_to_git(dirtied)
    assert bound is False and "changed" in why.lower()


def test_replaying_an_old_green_receipt_after_the_checkout_moves_is_refused(tmp_path):
    """The replay attack, end to end: a real green receipt, then the checkout advances."""
    base = _repo(tmp_path)
    rec = _green_record(base)
    assert v.is_green(rec) is True and v.matches_checkout(rec, base)[0] is True

    (base / "tests" / "test_y.py").write_text("def test_y():\n    assert True\n")
    for argv in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "y"]):
        subprocess.run(["git", *argv], cwd=base, capture_output=True)

    assert v.is_green(rec) is True, "the record is still internally coherent..."
    ok, why = v.matches_checkout(rec, base)
    assert ok is False, "...but it attests a checkout that is no longer on disk"
    assert "different checkout" in why


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
    """The only shape that may close a handoff: canonical argv, exit 0, bound to a real checkout.

    Shares :func:`test_done_contract.green_record` when a ``base`` is given, so the accepted shape is
    defined in exactly one place. With no base, a synthetic-but-complete git identity — used by the
    fail-closed table, which mutates one field at a time and asserts each mutation is refused.
    """
    if base is not None:
        return green_record(base)
    return {"kind": v.KIND_AUTHORITATIVE, "authoritative": True, "canonical_command": True,
            "exit_code": 0, "passed": True, "checkout_stable": True, "label": v.LABEL_GREEN,
            "command": list(v.AUTHORITATIVE_ARGV), "repo_root": "/repo",
            "start_sha": "a" * 40, "end_sha": "a" * 40, "start_status": "", "end_status": "",
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
    """A green receipt earned on an earlier commit must not close later work it never examined.

    The receipt is BUILT for this checkout rather than earned by running a stand-in command. The
    earlier version of this test called ``run_authoritative(base, [python, "-c", "sys.exit(0)"])`` and
    asserted ``is_green(rec) is True`` as its *precondition* — i.e. it depended on, and so quietly
    ratified, the fail-open that let any argv wear the authoritative label.
    """
    base = _repo(tmp_path)
    rec = _green_record(base)
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
    rec = _green_record(base)
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
