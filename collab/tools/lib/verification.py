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

**Identity, not output.** The gate is the argv, checked against :data:`AUTHORITATIVE_ARGV` before the
subprocess starts (:func:`is_canonical_command`). Trusting the command's *output* -- scanning stdout for
``RESULT: PASS`` -- would be worthless, because a narrow or hostile command prints whatever it likes;
trusting its *exit code alone* is what let ``python -c "print('RESULT: PASS')"`` earn the green label in
the first cut of this module. What a command cannot forge is which command it is.

**A checkout-state guard, not just a command.** An exit code attests the tree the command actually
ran against. If ``HEAD`` or ``git status`` moves between start and end, the run attests a checkout
that no longer exists, so :func:`is_green` refuses it even on exit 0. This is the concrete failure
mode from 2026-07-15: a builder mutating the tree while evidence was being captured.

**A git identity is mandatory.** ``None == None`` is not a SHA match. A source tree with no git (or no
commit) has no identity to pin a receipt to, so it cannot close autonomously -- :func:`is_bound_to_git`
refuses it with a reason, and :func:`matches_checkout` refuses to pin against it. Human review may still
inspect such a tree.

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
LABEL_NOT_CANONICAL = "REJECTED — not the authoritative command; refused before execution"
LABEL_NO_GIT = "VOID — not a git checkout; autonomous completion requires one"

_TIMEOUT_S = 3600

# Distinguishes "argv omitted -> use the canonical command" from "argv explicitly None". A caller
# writing run_authoritative(base, cfg.get("verify_command")) with the key absent means "I have no
# command", not "run the default"; silently upgrading that to the canonical run would hide the fact
# that nothing was configured. Refuse instead, and let the caller fall back deliberately.
_OMITTED = object()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_argv(argv) -> tuple[str, ...] | None:
    """Normalize an argv for comparison against :data:`AUTHORITATIVE_ARGV`; ``None`` if malformed.

    Deliberately minimal. The only normalisations are ones that cannot admit a *different* command:
    surrounding whitespace is stripped, and ``\\`` is folded to ``/`` so a Windows operator writing
    ``scripts\\verify.py`` names the same script as ``scripts/verify.py``. Nothing else is forgiven --
    no shell splitting, no PATH resolution, no flag reordering, no case folding. A ``str`` is malformed
    on purpose: ``"uv run ..."`` as one string would have to be shell-split, and a gate that shell-splits
    operator input is a gate that can be talked into running something else.
    """
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)):
        return None
    out: list[str] = []
    for token in argv:
        if not isinstance(token, str):
            return None
        cleaned = token.strip().replace("\\", "/")
        if not cleaned:
            return None
        out.append(cleaned)
    return tuple(out) if out else None


def is_canonical_command(argv) -> bool:
    """Is this EXACTLY the repository's authoritative whole-checkout gate?

    Equality against :data:`AUTHORITATIVE_ARGV`, not a prefix or a superset. ``scripts/verify.py
    --python-only`` is a different command with a different meaning (its own docstring: a narrowed
    invocation "never claims that the whole checkout is green"), and an extra argument is exactly how a
    narrowed run would be smuggled in wearing the authoritative label.
    """
    return normalize_argv(argv) == AUTHORITATIVE_ARGV


def _git(argv: list[str], base) -> str:
    try:
        p = subprocess.run(["git", *argv], cwd=str(base), capture_output=True, text=True, timeout=60)
        return p.stdout if p.returncode == 0 else ""
    except OSError, subprocess.SubprocessError:
        return ""


# Verifying mutates the tree: pytest writes __pycache__/.pytest_cache, ruff/mypy write their caches,
# and `uv run` may materialise .venv. Those are the verifier's own exhaust, not a change to the code
# under test, and comparing them would void every run (the target repo's .gitignore usually hides them
# -- but a gate must not depend on the audited repo being configured correctly).
_EPHEMERAL = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".coverage", ".venv")


def _significant(status: str) -> str:
    keep = [
        line for line in status.splitlines() if line.strip() and not any(frag in line for frag in _EPHEMERAL)
    ]
    return "\n".join(sorted(keep))


def _norm_root(root: str | None) -> str | None:
    """Canonical form of a git toplevel, for comparing two receipts' repositories."""
    if not root:
        return None
    try:
        return str(Path(root).resolve()).replace("\\", "/").casefold()
    except OSError, ValueError:
        return None


def _checkout_state(base) -> dict:
    """Repository root + ``HEAD`` + porcelain status: the identity of the tree a command runs against.

    ``root``/``sha`` are ``None`` when ``base`` is not a git checkout (or has no commit yet). That is a
    REFUSAL, not a free pass: see :func:`is_bound_to_git`. Status excludes the verifier's own cache
    exhaust (:data:`_EPHEMERAL`); everything else -- a tracked edit, a new untracked source file --
    counts as the checkout moving.
    """
    return {
        "root": _git(["rev-parse", "--show-toplevel"], base).strip() or None,
        "sha": _git(["rev-parse", "HEAD"], base).strip() or None,
        "status": _significant(_git(["status", "--porcelain"], base)),
    }


def _stable(start: dict, end: dict) -> bool:
    """Did the checkout hold still, AND was there a checkout to speak of at all?

    ``start["sha"] == end["sha"]`` alone is TRUE for ``None == None`` -- a non-git tree, where the
    comparison is vacuous. A receipt with no SHA attests nothing, so the identity must exist BEFORE
    equality means anything. This is the ``None == None`` fail-open (2026-07-15 audit).
    """
    return bool(
        start["sha"]
        and end["sha"]
        and start["root"]
        and end["root"]
        and start["sha"] == end["sha"]
        and _norm_root(start["root"]) == _norm_root(end["root"])
        and start["status"] == end["status"]
    )


def _record(kind, label, *, passed, command, exit_code, start, end, started_ts, ended_ts) -> dict:
    return {
        "kind": kind,
        "label": label,
        "passed": passed,
        "authoritative": kind == KIND_AUTHORITATIVE,
        "canonical_command": is_canonical_command(command),
        "command": list(command),
        "exit_code": exit_code,
        "started_ts": started_ts,
        "ended_ts": ended_ts,
        "repo_root": start["root"],
        "start_sha": start["sha"],
        "end_sha": end["sha"],
        "start_status": start["status"],
        "end_status": end["status"],
        "checkout_stable": _stable(start, end),
        "run_id": f"{kind}-{time.time_ns()}",
    }


def _run(argv, base, kind, pass_label, fail_label) -> dict:
    start, started_ts = _checkout_state(base), _now()
    try:
        proc = subprocess.run(list(argv), cwd=str(base), capture_output=True, text=True, timeout=_TIMEOUT_S)
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
    if passed and not _stable(start, end):
        # Exit 0 attests the tree the command ran on. Either that tree moved underneath it, or there
        # was never a git identity to attest in the first place -- name which, don't just say VOID.
        label = LABEL_NO_GIT if not (start["sha"] and start["root"]) else LABEL_CHECKOUT_MOVED
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


def rejected(argv, reason: str) -> dict:
    """A refusal record for a command that was NEVER RUN. Never authoritative, never green."""
    return {
        "kind": None,
        "label": f"{LABEL_NOT_CANONICAL} ({reason})",
        "passed": False,
        "authoritative": False,
        "canonical_command": False,
        "command": list(argv) if isinstance(argv, (list, tuple)) else [repr(argv)],
        "exit_code": None,
        "started_ts": None,
        "ended_ts": None,
        "repo_root": None,
        "start_sha": None,
        "end_sha": None,
        "start_status": None,
        "end_status": None,
        "checkout_stable": False,
        "run_id": None,
    }


def run_authoritative(base, argv=_OMITTED) -> dict:
    """Run the authoritative gate. The ONLY producer of a record :func:`is_green` accepts.

    ``argv`` is validated against :data:`AUTHORITATIVE_ARGV` and REFUSED BEFORE EXECUTION if it differs
    by so much as one argument. Passing ``argv`` at all only exists so a caller can be explicit; there
    is no argv other than the canonical one that this function will run.

    **Why this is not configurable.** The 2026-07-15 shape was: ``closeout.verify_command`` from
    seats.json was handed straight to ``subprocess`` and its exit code was stamped ``authoritative``.
    Anything exiting 0 -- ``python -c "print('RESULT: PASS')"``, a wrapper script, ``pytest`` on one
    file -- earned the label ``GREEN — scripts/verify.py exit 0`` and closed a handoff. Reading the
    command's OUTPUT instead would be no better: a malicious or narrow command prints whatever it likes.
    The command's IDENTITY is the only thing that cannot be forged by the command itself, so identity is
    what is checked, and it is checked here rather than trusted from config.

    The cost is real and accepted: collab-kit no longer honours a consuming repo's own gate command,
    which is a genuine loss for the domain-agnostic tenet (``docs/design/collab-kit-architecture.md:19``).
    Re-opening that seam needs a per-repo canonical declaration that is itself trusted evidence, not a
    bare string in the file the gated repo controls. See §18.3, the Autonomous Done-Transition Contract
    (``docs/design/collab-kit-architecture-autonomous.md:451``).
    """
    candidate = AUTHORITATIVE_ARGV if argv is _OMITTED else argv
    normalized = normalize_argv(candidate)
    if normalized is None:
        return rejected(candidate, "malformed argv: expected a non-empty list of non-empty strings")
    if normalized != AUTHORITATIVE_ARGV:
        return rejected(candidate, f"{' '.join(normalized)!r} != {' '.join(AUTHORITATIVE_ARGV)!r}")
    return _run(normalized, base, KIND_AUTHORITATIVE, LABEL_GREEN, LABEL_AUTHORITATIVE_FAIL)


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


def is_bound_to_git(record) -> tuple[bool, str]:
    """Is this receipt tied to a real, resolvable, motionless git checkout?

    Autonomous completion requires a git identity: a resolvable root, a non-null start AND end SHA that
    are equal, and a stable status. A non-git source tree yields ``None`` for root and SHA, and
    ``None == None`` is not a match -- it is the absence of the thing being matched. Human review may
    still inspect such a tree; it may not close autonomously.
    """
    if not isinstance(record, dict):
        return False, "no verification record"
    if not record.get("repo_root"):
        return False, "not a git checkout: no repository root resolved"
    if not record.get("start_sha") or not record.get("end_sha"):
        return False, "receipt carries no HEAD sha (not a git checkout, or no commit)"
    if record.get("start_sha") != record.get("end_sha"):
        return False, "HEAD moved while verifying"
    if record.get("start_status") != record.get("end_status"):
        return False, "working tree changed while verifying"
    if record.get("checkout_stable") is not True:
        return False, "checkout not stable across the run"
    return True, "receipt is bound to a stable git checkout"


def is_green(record) -> bool:
    """True ONLY for a CANONICAL authoritative exit-0 run over a real, motionless git checkout.

    The single place allowed to conclude "this checkout passed". Four independent things must hold, and
    each one was a way in before 2026-07-15:

    * ``kind``/``authoritative`` -- a pytest-only record can never satisfy it, whatever its ``passed``;
    * ``canonical_command`` -- the argv IS :data:`AUTHORITATIVE_ARGV`, so a wrapper that exits 0 (or
      prints ``RESULT: PASS``) cannot wear the label. Re-checked here, not trusted from the record:
      ``is_green`` is called on ledger JSON that a builder could have written;
    * ``exit_code``/``passed`` -- the gate actually returned 0;
    * :func:`is_bound_to_git` -- there was a real checkout, and it held still.
    """
    if not isinstance(record, dict):
        return False
    return (
        record.get("kind") == KIND_AUTHORITATIVE
        and record.get("authoritative") is True
        and is_canonical_command(record.get("command"))
        and record.get("exit_code") == 0
        and record.get("passed") is True
        and is_bound_to_git(record)[0]
    )


def matches_checkout(record, base) -> tuple[bool, str]:
    """Does this receipt attest the tree being closed RIGHT NOW, or some other one?

    :func:`is_green` proves a record is internally coherent -- the tree did not move *during* its
    own run. It cannot know whether that tree is still the one on disk. Without this, a receipt
    earned on an earlier checkout stays valid forever and can be replayed to close later work it
    never examined.

    A non-git tree is REFUSED, not excused. The previous cut compared ``record["end_sha"] != now["sha"]``
    and returned True when both were ``None``, so a directory with no git at all -- which cannot be
    pinned to anything -- satisfied the freshness check outright. Absence of identity is not proof of
    sameness.

    The repository root is compared too: a green receipt earned in a DIFFERENT repo that happens to sit
    at the same SHA (a fork, a mirror, a submodule) attests someone else's tree.
    """
    if not isinstance(record, dict):
        return False, "no verification record"
    now = _checkout_state(base)
    if not now["root"] or not now["sha"]:
        return False, f"source tree is not a git checkout ({base}); cannot pin a receipt to it"
    if not record.get("repo_root") or not record.get("end_sha"):
        return False, "receipt carries no git identity (no repo root / no sha)"
    if _norm_root(record.get("repo_root")) != _norm_root(now["root"]):
        return False, (
            f"receipt is from a different repository root: {record.get('repo_root')} != {now['root']}"
        )
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
