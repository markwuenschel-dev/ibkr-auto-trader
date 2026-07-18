"""Policy tests for the local verification matrix."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

_VERIFY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
_SPEC = importlib.util.spec_from_file_location("ibkr_verify_script", _VERIFY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
verify: Any = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = verify
_SPEC.loader.exec_module(verify)


def _args(**overrides: bool) -> argparse.Namespace:
    values = {
        "python_only": False,
        "no_build": False,
        "fail_fast": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _resolve_with(*, pnpm: str | None = "pnpm.cmd"):
    def resolve(program: str) -> str | None:
        return {"uv": "uv.exe", "pnpm": pnpm}.get(program)

    return resolve


def test_full_plan_is_locked_and_excludes_integration() -> None:
    plan = verify.build_plan(_args(), resolve=_resolve_with())

    assert [step.label for step in plan.steps] == [
        "core:lock",
        "core:pytest",
        "core:ruff",
        "core:pyright",
        "collab:pytest",
        "dash:test",
        "dash:lint",
        "dash:format",
        "dash:build",
    ]
    assert plan.omissions == ()

    core_pytest = next(step for step in plan.steps if step.label == "core:pytest")
    assert core_pytest.argv == ("uv.exe", "run", "--locked", "pytest", "-q", "-m", "not integration")
    # The integration exclusion is a CI-wide policy, not a core-only one: collab:pytest must carry
    # the same `-m "not integration"` filter so a future integration-marked collab test stays out
    # of CI by default (INT-033).
    collab_pytest = next(step for step in plan.steps if step.label == "collab:pytest")
    assert collab_pytest.argv == (
        "uv.exe",
        "run",
        "--locked",
        "pytest",
        "-q",
        "-m",
        "not integration",
        "collab/tests",
    )
    for step in plan.steps:
        if step.argv is not None and step.argv[:2] == ("uv.exe", "run"):
            assert step.argv[2] == "--locked"


def test_full_plan_fails_closed_when_dashboard_tooling_is_absent() -> None:
    plan = verify.build_plan(_args(), resolve=_resolve_with(pnpm=None))

    tooling = next(step for step in plan.steps if step.label == "dash:tooling")
    assert tooling.gate is True
    assert tooling.argv is None
    assert tooling.blocked_reason is not None
    assert "--python-only" in tooling.blocked_reason


def test_narrowed_plans_record_their_omitted_scope() -> None:
    python_only = verify.build_plan(_args(python_only=True), resolve=_resolve_with(pnpm=None))
    no_build = verify.build_plan(_args(no_build=True), resolve=_resolve_with())

    assert python_only.omissions == ("dashboard checks (--python-only)",)
    assert all(not step.label.startswith("dash:") for step in python_only.steps)
    assert no_build.omissions == ("dash:build (--no-build)",)
    assert all(step.label != "dash:build" for step in no_build.steps)


def test_default_main_returns_failure_when_pnpm_is_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verify, "_resolve", _resolve_with(pnpm=None))
    monkeypatch.setattr(verify, "run", lambda step: (step.blocked_reason is None, 0.0))

    assert verify.main([]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_narrowed_main_reports_partial_pass(monkeypatch, capsys) -> None:
    monkeypatch.setattr(verify, "_resolve", _resolve_with(pnpm=None))
    monkeypatch.setattr(verify, "run", lambda step: (True, 0.0))

    assert verify.main(["--python-only"]) == 0
    assert "RESULT: PARTIAL PASS" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# scope honesty: "full local matrix is green" is a claim about COVERAGE
# --------------------------------------------------------------------------- #


def test_ruff_gates_the_whole_repo_including_collab() -> None:
    """Ruff was `check src tests scripts/verify.py`, leaving 62 first-party collab/ files ungated
    while the run still printed "full local matrix is green". Whole-repo now, and GATING."""
    plan = verify.build_plan(_args(), resolve=_resolve_with())
    ruff = next(step for step in plan.steps if step.label == "core:ruff")

    assert ruff.argv == ("uv.exe", "run", "--locked", "ruff", "check", ".")
    assert ruff.gate is True, "the whole-repo lint must be a required gate, not a debt tracker"
    assert not any("collab:lint" in step.label for step in plan.steps), "no non-gating lint step"


def test_the_full_matrix_claim_is_withheld_while_first_party_code_is_ungated(monkeypatch, capsys) -> None:
    """The rule: "full matrix" and "first-party code intentionally ungated" may not both be true.

    pyright still does not cover collab/ (372 findings), so that omission is DECLARED and the sentence
    is withheld. This is the honest half of the trade — the Ruff gap was closed instead.
    """
    monkeypatch.setattr(verify, "_resolve", _resolve_with())
    monkeypatch.setattr(verify, "run", lambda step: (True, 0.0))

    assert verify.STANDING_OMISSIONS, "precondition: something first-party is still ungated"
    assert verify.main([]) == 0

    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "full local matrix is green" not in out, "must not claim coverage it does not have"
    assert "NOT a full-matrix claim" in out
    assert "collab/ type-checking" in out, "the gap must be named, not merely omitted"


def test_the_full_matrix_sentence_returns_only_when_nothing_is_ungated(monkeypatch, capsys) -> None:
    """Emptying STANDING_OMISSIONS is the only way to earn the sentence back."""
    monkeypatch.setattr(verify, "_resolve", _resolve_with())
    monkeypatch.setattr(verify, "run", lambda step: (True, 0.0))
    monkeypatch.setattr(verify, "STANDING_OMISSIONS", ())

    assert verify.main([]) == 0
    assert "RESULT: PASS (full local matrix is green)" in capsys.readouterr().out
