"""Focused tests for the risk-tiered assurance-plan resolver.

The resolver is deliberately independent of lane execution.  These tests pin the
configuration boundary that Task 2 will consume: a code candidate receives one
already-resolved immutable plan for both identity and execution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import verification_plan as vp  # noqa: E402, I001


_REPO_ADAPTER = "C:/repo/collab/tools/adapters/openai-repo-seat.py"
_TEXT_ADAPTER = "C:/repo/collab/tools/adapters/openai-compatible-seat.py"


def _seats_document(*, high_providers=("openai", "xai")) -> dict:
    """A complete, repo-capable four-role config with the required two profiles."""
    return {
        "version": 2,
        "assessment_profile_revision": "profiles-v1",
        "models": {
            "opus-4.8": {
                "provider": "anthropic",
                "cmd": ["claude", "-p", "--model", "opus-4.8"],
            },
            "sonnet-5": {
                "provider": "anthropic",
                "cmd": ["claude", "-p", "--model", "sonnet-5"],
            },
            "gpt-5.6-luna": {
                "provider": high_providers[0],
                "cmd": [
                    "python",
                    _REPO_ADAPTER,
                    "--base",
                    "https://api.openai.com/v1",
                    "--model",
                    "gpt-5.6-luna",
                    "--key-env",
                    "OPENAI_API_KEY",
                    "--repo-root",
                    "C:/repo",
                ],
            },
            "grok-4.5": {
                "provider": high_providers[1],
                "cmd": [
                    "python",
                    _REPO_ADAPTER,
                    "--base",
                    "https://api.x.ai/v1",
                    "--model",
                    "grok-4.5",
                    "--key-env",
                    "XAI_API_KEY",
                    "--repo-root",
                    "C:/repo",
                ],
            },
        },
        "seats": {
            "builder": {"backend": "cli", "role": "builder", "access": "write", "model": "opus-4.8"},
            "reviewer": {
                "backend": "cli",
                "role": "reviewer",
                "access": "read_test",
                "model": "sonnet-5",
            },
            "breaker": {
                "backend": "cli",
                "role": "breaker",
                "access": "read_test",
                "model": "opus-4.8",
            },
            "verifier": {
                "backend": "cli",
                "role": "verifier",
                "access": "read_test",
                "model": "sonnet-5",
            },
        },
        "assessment_profiles": {
            "baseline": {
                "breaker": {"seat": "breaker", "model": "opus-4.8"},
                "verifier": {"seat": "verifier", "model": "sonnet-5"},
            },
            "high-risk-diverse": {
                "extends": "baseline",
                "overrides": {
                    "breaker": {"model": "gpt-5.6-luna"},
                    "verifier": {"model": "grok-4.5"},
                },
            },
        },
    }


def _lanes_document() -> dict:
    return json.loads(
        (Path(__file__).resolve().parent.parent / "telemetry" / "lanes.json").read_text(encoding="utf-8")
    )


def _plan(guardrails=(), *, seats=None, lanes=None):
    return vp.resolve_verification_plan(
        lanes or _lanes_document(), seats or _seats_document(), guardrails=guardrails
    )


class TestLaneConfiguration:
    def test_v2_configuration_has_all_required_contracts(self):
        specs = vp.parse_lane_specs(_lanes_document())
        assert {spec.id for spec in specs} == {
            "change-regression",
            "untrusted-agent-output",
            "bounded-autonomy",
            "path-pointer-safety",
            "process-isolation",
            "data-integrity-under-concurrent-autopilots",
            "order-risk-and-idempotency",
            "market-data-causality-and-time",
            "broker-snapshot-and-reconciliation",
            "state-concurrency-and-retry",
        }

    def test_guardrails_are_normalized_and_deduplicated(self):
        plan = _plan(["  Market_Data ", "money", "MONEY"])
        assert plan.guardrails == ("market-data", "money")

    def test_rejects_an_unknown_high_risk_trigger(self):
        lanes = _lanes_document()
        lanes["lane_specs"][0]["selection"]["high-risk"] = ["not-a-risk-class"]
        with pytest.raises(vp.VerificationPlanError, match="high-risk"):
            vp.parse_lane_specs(lanes)

    def test_rejects_obsolete_v1_fanout_configuration(self):
        with pytest.raises(vp.VerificationPlanError, match="version 2"):
            vp.parse_lane_specs({"version": 1, "risk_classes": {}})

    def test_shipped_four_role_example_resolves_repo_capable_profiles(self):
        root = Path(__file__).resolve().parent.parent
        seats = json.loads((root / "seats.example.json").read_text(encoding="utf-8"))
        plan = vp.resolve_verification_plan(_lanes_document(), seats, guardrails=["money"])
        assert plan.baseline.profile.breaker_model == "gemini-3.5-flash"
        assert plan.baseline.profile.verifier_model == "anthropic-general"
        assert plan.high_risk is not None
        assert plan.high_risk.profile.breaker_model == "gpt-5.6-luna"
        assert plan.high_risk.profile.verifier_model == "grok-4.5"
        assert "--run-checks" in plan.high_risk.profile.breaker_cmd
        assert "--run-checks" in plan.high_risk.profile.verifier_cmd


class TestSelection:
    def test_baseline_is_present_for_every_candidate(self):
        plan = _plan()
        assert len(plan.passes) == 1
        assert plan.baseline.contract_ids == ("change-regression",)
        assert plan.high_risk is None

    def test_non_high_generic_contracts_join_baseline(self):
        plan = _plan(["untrusted_agent_output", "bounded-autonomy", "path safety"])
        assert plan.baseline.contract_ids == (
            "bounded-autonomy",
            "change-regression",
            "path-pointer-safety",
            "untrusted-agent-output",
        )
        assert plan.high_risk is None

    @pytest.mark.parametrize(
        ("guardrail", "expected", "baseline"),
        [
            ("money", {"order-risk-and-idempotency"}, {"change-regression"}),
            ("execution", {"order-risk-and-idempotency"}, {"change-regression"}),
            ("auth", {"path-pointer-safety"}, {"change-regression", "path-pointer-safety"}),
            ("broker", {"broker-snapshot-and-reconciliation"}, {"change-regression"}),
            (
                "integration",
                {"broker-snapshot-and-reconciliation", "process-isolation"},
                {"change-regression", "process-isolation"},
            ),
            ("market-data", {"market-data-causality-and-time"}, {"change-regression"}),
            ("time", {"market-data-causality-and-time"}, {"change-regression"}),
            (
                "data-integrity",
                {"data-integrity-under-concurrent-autopilots", "state-concurrency-and-retry"},
                {"change-regression", "data-integrity-under-concurrent-autopilots"},
            ),
            (
                "concurrency",
                {"data-integrity-under-concurrent-autopilots", "state-concurrency-and-retry"},
                {"change-regression", "data-integrity-under-concurrent-autopilots"},
            ),
        ],
    )
    def test_each_safety_critical_guardrail_selects_the_high_risk_pass(self, guardrail, expected, baseline):
        plan = _plan([guardrail])
        assert set(plan.baseline.contract_ids) == baseline
        assert plan.high_risk is not None
        assert set(plan.high_risk.contract_ids) == expected

    def test_high_risk_contracts_are_one_composite_pass(self):
        plan = _plan(["money", "market-data", "broker", "concurrency"])
        assert len(plan.passes) == 2
        assert plan.high_risk is not None and plan.high_risk.composite is True
        assert set(plan.high_risk.contract_ids) == {
            "broker-snapshot-and-reconciliation",
            "data-integrity-under-concurrent-autopilots",
            "market-data-causality-and-time",
            "order-risk-and-idempotency",
            "state-concurrency-and-retry",
        }
        assert plan.high_risk.composite_payload["contracts"] == list(plan.high_risk.contract_ids)


class TestProfileValidation:
    def test_high_risk_provider_overlap_fails_closed(self):
        with pytest.raises(vp.VerificationPlanError, match="disjoint"):
            _plan(["money"], seats=_seats_document(high_providers=("anthropic", "xai")))

    def test_identical_breaker_and_verifier_execution_fingerprints_fail(self):
        seats = _seats_document()
        seats["assessment_profiles"]["baseline"]["verifier"]["model"] = "opus-4.8"
        with pytest.raises(vp.VerificationPlanError, match="execution fingerprints"):
            _plan(seats=seats)

    def test_text_only_assessment_executor_is_rejected_before_runtime(self):
        seats = _seats_document()
        seats["models"]["sonnet-5"] = {
            "provider": "anthropic",
            "cmd": ["python", _TEXT_ADAPTER, "--model", "sonnet-5"],
        }
        with pytest.raises(vp.VerificationPlanError, match=r"repository-capable|read_test"):
            _plan(seats=seats)

    def test_reviewer_must_also_be_a_repo_capable_read_test_assessor(self):
        seats = _seats_document()
        seats["seats"]["reviewer"]["access"] = "read"
        with pytest.raises(vp.VerificationPlanError, match=r"reviewer.*read_test"):
            _plan(seats=seats)


class TestIdentityPayload:
    def test_identity_is_stable_for_equivalent_input(self):
        left = _plan(["money", "market-data"])
        right = _plan(["MARKET_DATA", "Money", "money"])
        assert left.identity_payload == right.identity_payload
        assert left.identity_digest == right.identity_digest

    def test_identity_changes_with_selected_profile_or_prompt_revision(self):
        baseline = _plan()
        high = _plan(["money"])
        changed_prompt = _lanes_document()
        changed_prompt["prompt_revision"] = "assurance-v2-revised"
        changed = _plan(lanes=changed_prompt)
        assert baseline.identity_payload != high.identity_payload
        assert baseline.identity_payload != changed.identity_payload
