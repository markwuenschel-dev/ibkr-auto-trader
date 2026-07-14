"""Focused execution tests for the bounded, resolved assurance plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import collab_common as cc  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import run_budget as rb  # noqa: E402
import verification_plan as vp  # noqa: E402


def _profile(name: str) -> vp.AssessmentProfile:
    return vp.AssessmentProfile(
        id=name,
        breaker_seat=f"{name}-breaker",
        verifier_seat=f"{name}-verifier",
        breaker_model=f"{name}-breaker-model",
        verifier_model=f"{name}-verifier-model",
        breaker_provider=f"{name}-provider",
        verifier_provider=f"{name}-verify-provider",
        breaker_execution_fingerprint=f"exec:{name}-breaker",
        verifier_execution_fingerprint=f"exec:{name}-verifier",
        fingerprint=f"profile:{name}",
        breaker_cmd=(f"fake-{name}-breaker",),
        verifier_cmd=(f"fake-{name}-verifier",),
    )


def _plan(*, high: bool = False) -> vp.VerificationPlan:
    baseline_spec = vp.LaneSpec(
        id="change-regression",
        title="Change regression",
        category="generic",
        checklist=("probe",),
        baseline_guardrails=(),
        high_risk_guardrails=(),
        always_baseline=True,
        revision="v2",
    )
    baseline = vp.LanePass("baseline", _profile("baseline"), (baseline_spec,), False)
    high_pass = None
    guardrails = ()
    if high:
        specialist = vp.LaneSpec(
            id="order-risk-and-idempotency",
            title="Order",
            category="trading",
            checklist=("probe",),
            baseline_guardrails=(),
            high_risk_guardrails=("money",),
            revision="v2",
        )
        high_pass = vp.LanePass("high-risk-diverse", _profile("high"), (specialist,), True)
        guardrails = ("money",)
    payload = json.dumps({"high": high}, sort_keys=True)
    return vp.VerificationPlan(
        lane_config_revision="v2",
        prompt_revision="prompt-v1",
        assessment_profile_revision="profiles-v1",
        guardrails=guardrails,
        baseline=baseline,
        high_risk=high_pass,
        identity_payload=payload,
        identity_digest=f"plan:{'high' if high else 'baseline'}",
    )


def _budget(tmp_path):
    return rb.RunBudget(
        str(tmp_path / "budget"),
        "001",
        rb.Limits(
            max_work_attempts=3,
            max_verification_passes=6,
            max_total_model_calls=18,
            max_wall_clock_seconds=60.0,
            max_findings_per_lane=3,
        ),
    )


def _handoff(collab):
    hc.create(collab, to="reviewer", from_="builder", title="review", body="change under test")


def test_resolved_plan_uses_one_batched_verifier_per_pair(tmp_path):
    collab = str(tmp_path / "collab")
    _handoff(collab)
    calls = []

    def runner(cmd, prompt, *, timeout, **kw):
        calls.append(cmd[0])
        if "breaker" in cmd[0]:
            return "FINDING: F1 | src/order.py:9 | duplicate retry | duplicate order"
        return "VERDICT: CONFIRMED F1 | src/order.py:9 | duplicate retry reproduces"

    budget = _budget(tmp_path)
    ledger = lanes.run_lanes(
        collab,
        "001",
        seats={},
        breaker_seat="unused",
        verifier_seat="unused",
        builder_seat="builder",
        reviewer_seat="reviewer",
        runner=runner,
        budget=budget,
        candidate_id="cand:plan",
        verification_plan=_plan(high=True),
    )

    assert len(calls) == 4  # breaker + verifier for baseline, then for the one composite specialist pair
    assert budget.consumed()["verification_passes"] == 2
    assert budget.consumed()["verification_calls"] == 4
    assert ledger["reviewer_seat"] == "reviewer"
    assert ledger["verification_plan_digest"] == "plan:high"
    assert {blocker["lane"] for blocker in ledger["blockers"]} == {"baseline", "high-risk-diverse"}


def test_missing_batch_verdict_is_incomplete_not_a_pass(tmp_path):
    collab = str(tmp_path / "collab")
    _handoff(collab)

    def runner(cmd, prompt, *, timeout, **kw):
        if "breaker" in cmd[0]:
            return "FINDING: F1 | src/x.py:1 | trigger | impact"
        return "VERDICT: CONFIRMED F2 | src/x.py:1 | wrong id"

    ledger = lanes.run_lanes(
        collab,
        "001",
        seats={},
        breaker_seat="unused",
        verifier_seat="unused",
        builder_seat="builder",
        runner=runner,
        candidate_id="cand:incomplete",
        verification_plan=_plan(),
    )

    assert ledger["incomplete"] is True
    assert ledger["blockers"] == []
    assert "verifier" in ledger["lanes"][0]["incomplete"]["reason"]


def test_batch_protocol_prose_is_incomplete_not_silently_accepted(tmp_path):
    collab = str(tmp_path / "collab")
    _handoff(collab)

    def runner(cmd, prompt, *, timeout, **kw):
        if "breaker" in cmd[0]:
            return "NO-FINDING\nI also looked at the diff."
        raise AssertionError("malformed no-finding output must not dispatch a verifier")

    ledger = lanes.run_lanes(
        collab,
        "001",
        seats={},
        breaker_seat="unused",
        verifier_seat="unused",
        builder_seat="builder",
        runner=runner,
        candidate_id="cand:strict",
        verification_plan=_plan(),
    )

    assert ledger["incomplete"] is True
    assert ledger["lanes"][0]["ran"] is False
    assert "prose" in ledger["lanes"][0]["incomplete"]["reason"]


def test_unavailable_diverse_profile_is_infrastructure_blocked_not_baseline_fallback(tmp_path):
    collab = str(tmp_path / "collab")
    _handoff(collab)

    def runner(cmd, prompt, *, timeout, **kw):
        if "high-breaker" in cmd[0]:
            raise cc.CollabError("configured provider unavailable")
        return "NO-FINDING"

    ledger = lanes.run_lanes(
        collab,
        "001",
        seats={},
        breaker_seat="unused",
        verifier_seat="unused",
        builder_seat="builder",
        runner=runner,
        candidate_id="cand:unavailable",
        verification_plan=_plan(high=True),
    )

    assert {entry["pass"] for entry in ledger["lanes"]} == {"baseline", "high-risk-diverse"}
    assert ledger["tool_error"]["seat"] == "high-breaker"
    assert ledger["lanes"][0]["ran"] is True or ledger["lanes"][1]["ran"] is True


def test_candidate_identity_binds_the_resolved_assurance_plan(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "module.py").write_text("x = 1\n", encoding="utf-8")
    seats = {
        "builder": {"backend": "cli", "cmd": ["builder"], "system": "builder"},
        "reviewer": {"backend": "cli", "cmd": ["reviewer"], "system": "reviewer"},
    }
    common = dict(
        seats=seats,
        builder_seat="builder",
        reviewer_seat="reviewer",
        source_roots=["src/*.py"],
        source_base=str(tmp_path),
        test_path="tests",
        guardrails=[],
        builder_output="output",
    )
    baseline = ap._compute_candidate(str(tmp_path), "001", verification_plan=_plan(), **common)
    high = ap._compute_candidate(str(tmp_path), "001", verification_plan=_plan(high=True), **common)

    assert baseline.candidate_id != high.candidate_id
    assert baseline.assessment_plan_revision == "v2"
