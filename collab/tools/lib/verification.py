"""Authoritative vs partial verification, and the terminology that separates them.

``scripts/verify.py`` (unflagged) is the ONLY command that can establish that a checkout is
green -- ``.github/workflows/verify.yml`` runs it and nothing else, and its own module docstring
says narrowed invocations "never claim that the whole checkout is green". Everything else is a
PARTIAL result.

A bare ``pytest`` run is the partial result that matters here: it exercises tests and says nothing
about lint or types. On 2026-07-15 the done-gate's only automated evidence was such a run
(``autopilot._run_test_suite`` -> ``pytest <test_path> -q``), and ``done_contract`` condition 5 read
its boolean ``passed`` as though it attested the checkout. It never did. The words are the fix:

  * :data:`LABEL_GREEN`       -- earned ONLY by :func:`run_authoritative` exiting 0 over an
                                 unchanged checkout.
  * :data:`LABEL_PYTEST_ONLY` -- what a passing pytest-only run is allowed to say, and no more.

**A checkout-state guard, not just a command.** An exit code attests the tree the command actually
ran against. If ``HEAD`` or ``git status`` moves between start and end, the run attests a checkout
that no longer exists, so :func:`is_green` refuses it even on exit 0. This is the concrete failure
mode from 2026-07-15: a builder mutating the tree while evidence was being captured.

:func:`is_green` is the single reader. ``kind == "authoritative"`` alone is never sufficient, and
by construction :func:`run_pytest_only` cannot emit a record it accepts -- see
``collab/tests/test_verification.py``, which asserts that as a property.
"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

# The authoritative command. Unflagged on purpose: any narrowing flag makes it partial.
AUTHORITATIVE_ARGV: tuple[str, ...] = ("uv", "run", "--locked", "python", "scripts/verify.py")

KIND_AUTHORITATIVE = "authoritative"
KIND_PYTEST_ONLY = "pytest-only"

LABEL_GREEN = "GREEN — scripts/verify.py exit 0"
LABEL_AUTHORITATIVE_FAIL = "FAIL — scripts/verify.py did not exit 0"
LABEL_PYTEST_ONLY = "PYTEST PASS — lint and types not checked"
LABEL_PYTEST_ONLY_FAIL = "PYTEST FAIL — lint and types not checked"
LABEL_UNVERIFIED = "UNVERIFIED — no verification command ran"
LABEL_CHECKOUT_MOVED = "VOID — checkout changed while verifying; result attests nothing"

_TIMEOUT_S = 3600


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(argv: list[str], base) -> str:
    try:
        p = subprocess.run(
            ["git", *argv], cwd=str(base), capture_output=True, text=True, timeout=60
        )
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# Verifying mutates the tree: pytest writes __pycache__/.pytest_cache, ruff/mypy write their caches.
# Those are the verifier's own exhaust, not a change to the code under test, and comparing them would
# void every run (the target repo's .gitignore usually hides them -- but a gate must not depend on
# the audited repo being configured correctly).
_EPHEMERAL = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".coverage")


def _significant(status: str) -> str:
    keep = [
        line
        for line in status.splitlines()
        if line.strip() and not any(frag in line for frag in _EPHEMERAL)
    ]
    return "\n".join(sorted(keep))


def _checkout_state(base) -> dict:
    """``HEAD`` + porcelain status: the identity of the tree a command is about to run against.

    Status excludes the verifier's own cache exhaust (:data:`_EPHEMERAL`); everything else -- a
    tracked edit, a new untracked source file -- counts as the checkout moving.
    """
    return {
        "sha": _git(["rev-parse", "HEAD"], base).strip() or None,
        "status": _significant(_git(["status", "--porcelain"], base)),
    }


def _record(kind, label, *, passed, command, exit_code, start, end, started_ts, ended_ts) -> dict:
    return {
        "kind": kind,
        "label": label,
        "passed": passed,
        "authoritative": kind == KIND_AUTHORITATIVE,
        "command": list(command),
        "exit_code": exit_code,
        "started_ts": started_ts,
        "ended_ts": ended_ts,
        "start_sha": start["sha"],
        "end_sha": end["sha"],
        "start_status": start["status"],
        "end_status": end["status"],
        "checkout_stable": start["sha"] == end["sha"] and start["status"] == end["status"],
        "run_id": f"{kind}-{time.time_ns()}",
    }


def _run(argv, base, kind, pass_label, fail_label) -> dict:
    start, started_ts = _checkout_state(base), _now()
    try:
        proc = subprocess.run(
            list(argv), cwd=str(base), capture_output=True, text=True, timeout=_TIMEOUT_S
        )
        exit_code = proc.returncode
    except (OSError, subprocess.SubprocessError) as e:
        end = _checkout_state(base)
        return _record(
            kind,
            f"{fail_label} (runner error: {str(e)[:120]})",
            passed=False,
            command=argv,
            exit_code=-1,
            start=start,
            end=end,
            started_ts=started_ts,
            ended_ts=_now(),
        )
    end, ended_ts = _checkout_state(base), _now()
    passed = exit_code == 0
    label = pass_label if passed else fail_label
    if passed and not (start["sha"] == end["sha"] and start["status"] == end["status"]):
        # The tree moved under the command: exit 0 describes a checkout that no longer exists.
        label = LABEL_CHECKOUT_MOVED
        passed = False
    return _record(
        kind,
        label,
        passed=passed,
        command=argv,
        exit_code=exit_code,
        start=start,
        end=end,
        started_ts=started_ts,
        ended_ts=ended_ts,
    )


def run_authoritative(base, argv=None) -> dict:
    """Run the authoritative gate. The ONLY producer of a record :func:`is_green` accepts.

    ``argv`` defaults to :data:`AUTHORITATIVE_ARGV` (this repo's ``scripts/verify.py``). collab-kit is
    agent- and domain-agnostic, so a consuming repo declares its own via ``closeout.verify_command``
    in seats.json. What may NOT vary: whatever it is, it must be the repo's whole-checkout gate --
    a narrowed command that returns 0 for a subset is exactly the confusion this module exists to
    prevent.
    """
    return _run(
        tuple(argv) if argv else AUTHORITATIVE_ARGV,
        base,
        KIND_AUTHORITATIVE,
        LABEL_GREEN,
        LABEL_AUTHORITATIVE_FAIL,
    )


def run_pytest_only(test_path, base, *, python) -> dict:
    """Run ``pytest <test_path> -q``: a PARTIAL result that says so in its own label.

    Structurally incapable of producing a green record -- ``kind`` is never ``authoritative``.
    """
    if not test_path:
        return unverified("no test_path configured")
    argv = (python, "-m", "pytest", str(test_path), "-q")
    return _run(argv, base, KIND_PYTEST_ONLY, LABEL_PYTEST_ONLY, LABEL_PYTEST_ONLY_FAIL)


def unverified(reason: str = "") -> dict:
    detail = f"{LABEL_UNVERIFIED}{f' ({reason})' if reason else ''}"
    return {
        "kind": None,
        "label": detail,
        "passed": None,
        "authoritative": False,
        "command": [],
        "exit_code": None,
        "started_ts": None,
        "ended_ts": None,
        "start_sha": None,
        "end_sha": None,
        "start_status": None,
        "end_status": None,
        "checkout_stable": None,
        "run_id": None,
    }


def is_green(record) -> bool:
    """True ONLY for an authoritative exit-0 run over a checkout that did not move.

    The single place allowed to conclude "this checkout passed". A pytest-only record can never
    satisfy it, whatever its ``passed`` value.
    """
    if not isinstance(record, dict):
        return False
    return (
        record.get("kind") == KIND_AUTHORITATIVE
        and record.get("authoritative") is True
        and record.get("exit_code") == 0
        and record.get("passed") is True
        and record.get("checkout_stable") is True
    )


def matches_checkout(record, base) -> tuple[bool, str]:
    """Does this receipt attest the tree being closed RIGHT NOW, or some other one?

    :func:`is_green` proves a record is internally coherent -- the tree did not move *during* its
    own run. It cannot know whether that tree is still the one on disk. Without this, a receipt
    earned on an earlier checkout stays valid forever and can be replayed to close later work it
    never examined.

    ``sha is None`` on BOTH sides means the repo under review is not a git checkout at all; there is
    no identity to compare, so this cannot speak and defers to done-contract condition 8 (source
    mtime vs ledger timestamp). A None/non-None pair is a genuine mismatch.
    """
    if not isinstance(record, dict):
        return False, "no verification record"
    now = _checkout_state(base)
    if record.get("end_sha") != now["sha"]:
        return False, (
            f"receipt attests a different checkout: {str(record.get('end_sha'))[:12]} "
            f"!= current {str(now['sha'])[:12]}"
        )
    if record.get("end_status") != now["status"]:
        return False, "working tree changed after the receipt was written"
    return True, "receipt attests the current checkout"


def label_of(record) -> str:
    if not isinstance(record, dict) or not record.get("label"):
        return LABEL_UNVERIFIED
    return record["label"]


def verify_script_present(base) -> bool:
    return (Path(base) / "scripts" / "verify.py").is_file()
