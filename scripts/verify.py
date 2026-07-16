#!/usr/bin/env python3
"""Fail-closed local aggregate verification for ibkr-auto-trader.

The unflagged invocation is the only command that can establish that every
required gate is green. Explicitly narrowed commands return zero when their
requested checks pass, but report ``PARTIAL PASS`` and never claim that the
whole checkout is green.

What "green" is allowed to mean is bounded by :data:`STANDING_OMISSIONS`: while
first-party code is knowingly ungated, no invocation prints "full local matrix
is green", because that sentence is a claim about coverage and not merely about
exit codes. Ruff gates the whole repo; pyright does not yet cover ``collab/``.

Run it:  uv run --locked python scripts/verify.py
Flags:   --python-only  omit every dashboard check (partial result)
         --no-build     omit the dashboard production build (partial result)
         --fail-fast    stop at the first failed required check
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# First-party code this matrix knowingly does NOT gate. Declared here, printed on every run, and
# load-bearing: while this tuple is non-empty, no invocation may print "full local matrix is green".
# Emptying it is the only way to earn that sentence back.
#
# pyright's include is ["src", "tests", "scripts"] (pyproject.toml). Pointing it at collab/ today
# reports 372 errors — a real debt, not a config slip, and far too large to clear as a side effect of
# an unrelated change. Ruff, by contrast, WAS excluded on the same "pre-existing debt" reasoning; the
# debt turned out to be 5 findings, so it is now gated whole-repo.
STANDING_OMISSIONS: tuple[str, ...] = (
    "collab/ type-checking (pyright covers src, tests, scripts only; 372 known findings)",
)


@dataclass(frozen=True)
class Step:
    """One verification action, or an explicit failure when tooling is absent."""

    label: str
    argv: tuple[str, ...] | None = None
    gate: bool = True
    blocked_reason: str | None = None


@dataclass(frozen=True)
class VerificationPlan:
    """Required actions plus explicit, user-requested scope omissions."""

    steps: tuple[Step, ...]
    omissions: tuple[str, ...] = ()


def _resolve(program: str) -> str | None:
    """Resolve an executable, including ``pnpm.cmd`` / ``uv.exe`` on Windows."""
    return shutil.which(program)


def _uv_run(label: str, uv: str, *command: str, gate: bool = True) -> Step:
    return Step(label, (uv, "run", "--locked", *command), gate=gate)


def build_plan(
    args: argparse.Namespace,
    *,
    resolve: Callable[[str], str | None] = _resolve,
) -> VerificationPlan:
    """Build the matrix without running it, keeping missing required tools visible."""
    uv = resolve("uv")
    if uv is None:
        return VerificationPlan(
            (
                Step(
                    "python:tooling",
                    blocked_reason="`uv` is not on PATH; Python gates cannot be verified.",
                ),
            )
        )

    steps: list[Step] = [
        # Verify the lock before any `uv run` can use the dependency graph.
        Step("core:lock", (uv, "lock", "--check")),
        _uv_run("core:pytest", uv, "pytest", "-q", "-m", "not integration"),
        # Whole-repo, and GATING. This was `ruff check src tests scripts/verify.py` with `ruff check .`
        # available only behind --include-collab-lint as a non-gating "debt tracker": 62 first-party
        # .py files under collab/ were linted by nothing while the run still printed "full local matrix
        # is green". The debt that justified it was recorded as ~321 findings; when actually measured on
        # 2026-07-15 it was 5, four of them autofixable. Ruff's default respect-gitignore keeps the
        # generated autopilot/ output out of scope.
        _uv_run("core:ruff", uv, "ruff", "check", "."),
        _uv_run("core:pyright", uv, "pyright"),
        _uv_run("collab:pytest", uv, "pytest", "-q", "collab/tests"),
    ]

    omissions: list[str] = []
    if args.python_only:
        omissions.append("dashboard checks (--python-only)")
    else:
        pnpm = resolve("pnpm")
        if pnpm is None:
            steps.append(
                Step(
                    "dash:tooling",
                    blocked_reason=(
                        "`pnpm` is not on PATH; default verification requires dashboard checks. "
                        "Use --python-only only when a partial result is intended."
                    ),
                )
            )
        else:
            steps.extend(
                (
                    Step("dash:test", (pnpm, "--dir", "dashboard", "test")),
                    Step("dash:lint", (pnpm, "--dir", "dashboard", "lint")),
                    Step("dash:format", (pnpm, "--dir", "dashboard", "format:check")),
                )
            )
            if args.no_build:
                omissions.append("dash:build (--no-build)")
            else:
                steps.append(Step("dash:build", (pnpm, "--dir", "dashboard", "build")))

    return VerificationPlan(tuple(steps), tuple(omissions))


def run(step: Step) -> tuple[bool, float]:
    """Run one step; unavailable tooling is a required-check failure, never a skip."""
    if step.blocked_reason is not None:
        print(f"BLOCKED: {step.blocked_reason}", file=sys.stderr)
        return False, 0.0

    assert step.argv is not None
    start = time.monotonic()
    try:
        proc = subprocess.run(step.argv, cwd=REPO_ROOT)  # inherit output for actionable failures
    except OSError as exc:
        print(f"BLOCKED: could not start {step.argv[0]!r}: {exc}", file=sys.stderr)
        return False, time.monotonic() - start
    return proc.returncode == 0, time.monotonic() - start


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed local verification for ibkr-auto-trader.")
    parser.add_argument(
        "--python-only",
        action="store_true",
        help="omit every dashboard check and report a partial result",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="omit the dashboard production build and report a partial result",
    )
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failed gate")
    # --include-collab-lint is gone: whole-repo Ruff is now a required gate (core:ruff), not an
    # opt-in tracker. It was also quietly worthless — the step it added was gate=False AND added no
    # omission, so a failing collab lint still printed "RESULT: PASS (full local matrix is green)".
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    plan = build_plan(args, resolve=_resolve)
    results: list[tuple[Step, bool, float]] = []

    for step in plan.steps:
        print(f"\n=== {step.label} ===", flush=True)
        ok, elapsed = run(step)
        results.append((step, ok, elapsed))
        if not ok and step.gate and args.fail_fast:
            print(f"\n[fail-fast] {step.label} FAILED after {elapsed:.1f}s")
            break

    print("\n" + "=" * 60)
    print("VERIFY MATRIX")
    print("=" * 60)
    gate_failures = 0
    for step, ok, elapsed in results:
        mark = "PASS" if ok else "FAIL"
        tag = "  (non-gating)" if not step.gate else ""
        print(f"  {mark:4}  {step.label:<34} {elapsed:6.1f}s{tag}")
        if step.gate and not ok:
            gate_failures += 1

    executed = {step.label for step, _, _ in results}
    not_run = [step.label for step in plan.steps if step.label not in executed]
    if not_run:
        print(f"\n  Not run (fail-fast): {', '.join(not_run)}")
    if plan.omissions:
        print(f"\n  Intentional scope omissions: {', '.join(plan.omissions)}")

    if STANDING_OMISSIONS:
        print(f"\n  Standing scope omissions: {'; '.join(STANDING_OMISSIONS)}")
    print("  Never invoked here: broker/integration tests, dashboard Playwright e2e")

    if gate_failures:
        print(f"\nRESULT: FAIL ({gate_failures} required check(s) failed)")
        return 1
    if plan.omissions:
        print("\nRESULT: PARTIAL PASS (requested scope is green; full checkout is not asserted)")
        return 0
    if STANDING_OMISSIONS:
        # "full local matrix is green" is a claim about coverage, not just about exit codes, and it may
        # not be made while first-party code is knowingly ungated. Ruff now covers everything; pyright
        # does not cover collab/. Say so instead of rounding up.
        print(
            "\nRESULT: PASS (every required gate is green; NOT a full-matrix claim — "
            f"{'; '.join(STANDING_OMISSIONS)})"
        )
        return 0
    print("\nRESULT: PASS (full local matrix is green)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
