"""Human run-summary projection with explicit evidence authority and gaps."""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import run_summary as rs  # noqa: E402


def test_summary_separates_facts_judgments_model_claims_and_missing_evidence() -> None:
    projection = {
        "run_uid": "run-1",
        "plan": {
            "objective": "Make the run explainable",
            "rounds": {"maximum": 3},
        },
        "attempts": [
            {
                "attempt_id": "attempt-1",
                "source": "gateway_client",
                "seat": "builder",
                "requested_model": "gpt-5.6-luna",
                "state": "completed",
                "states": ["connecting", "gateway_accepted", "completed"],
                "telemetry_state": "telemetry_verified",
                "candidate_id": "candidate-a",
            },
            {
                "attempt_id": "attempt-2",
                "source": "gateway_client",
                "seat": "reviewer",
                "requested_model": "grok-4.5",
                "state": "completed",
                "states": ["connecting", "gateway_accepted", "completed"],
                "telemetry_state": None,
                "candidate_id": None,
            },
        ],
        "roster": [
            {"role": "builder", "model": "gpt-5.6-luna", "state": "telemetry_reconciled"},
            {"role": "reviewer", "model": "grok-4.5", "state": "provider_returned"},
            {"role": "verifier", "model": "haiku-4.5", "state": "selected"},
        ],
        "latest_decision": {
            "completed_round": 2,
            "maximum_rounds": 3,
            "decision": "stop",
            "reason": "accepted_result_met_completion_criteria",
        },
        "candidates": [
            {
                "candidate_id": "candidate-a",
                "files": ["a.py"],
                "current_disposition": "accepted",
                "parent_candidate_id": "candidate-base",
                "patch_digest": "sha256:patch",
                "final_artifact_ref": "source-manifest:sha256:state",
                "final_commit": "abc123",
                "revision_evidence_refs": ["finding:f1"],
            }
        ],
        "validations": [
            {
                "validation_id": "pytest",
                "candidate_id": "candidate-a",
                "source_kind": "automated_check",
                "status": "passed",
                "is_acceptance_oracle": True,
                "artifact_ref": "pytest.txt",
                "producer": "pytest",
                "producer_version": "9.1",
                "dimensions": {"correctness": "passed", "integration": "warning"},
                "baseline_delta": {"before": "failed", "after": "passed"},
                "uncertainty": "browser scope not included",
                "gaps": ["performance benchmark missing"],
                "test_quality": {"fails_before_fix": True, "avoids_over_mocking": True},
            },
            {
                "validation_id": "self-report",
                "candidate_id": "candidate-a",
                "source_kind": "model_self_report",
                "status": "passed",
                "artifact_ref": "claim:1",
            },
        ],
        "requirements": [
            {
                "requirement_id": "REQ-1",
                "candidate_id": "candidate-a",
                "critical": True,
                "effective_status": "met",
            }
        ],
        "dispositions": [
            {
                "candidate_id": "candidate-a",
                "disposition": "accepted",
                "executive_explanation": "The named oracle passed.",
                "weaknesses": ["No performance benchmark"],
                "unavailable_evidence": ["fresh xAI provider success"],
                "human_review_triggers": ["provider evidence changes"],
                "decision_maker": "done-contract",
                "primary_reason": None,
                "reason_categories": [],
                "failed_checks": [],
                "remediation": "Retain the performance warning.",
                "resolution": "The named oracle passed all critical requirements.",
                "requirements_evaluation": {"eligible": True, "critical_met": 1},
            }
        ],
        "evidence_health": {"archive_integrity": "healthy", "lifecycle_conflicts": 0},
    }
    summary = rs.build(projection, legacy_summary={"rounds_total": 4})

    assert summary["outcome"]["stop_reason"] == "accepted_result_met_completion_criteria"
    assert summary["outcome"]["completed_rounds"] == 2
    assert summary["outcome"]["maximum_rounds"] == 3
    assert summary["proven_facts"]["models_attempted"] == ["gpt-5.6-luna", "grok-4.5"]
    assert summary["proven_facts"]["calls_reaching_litellm"] == 2
    assert summary["proven_facts"]["calls_reaching_langfuse"] == 1
    assert summary["proven_facts"]["provider_responses"] == 2
    assert summary["evaluator_judgments"][0]["candidate_id"] == "candidate-a"
    assert summary["evaluator_judgments"][0]["decision_maker"] == "done-contract"
    assert summary["evaluator_judgments"][0]["requirements_evaluation"]["eligible"] is True
    automated = summary["proven_facts"]["automated_validations"][0]
    assert automated["dimensions"] == {"correctness": "passed", "integration": "warning"}
    assert automated["baseline_delta"] == {"before": "failed", "after": "passed"}
    assert automated["test_quality"]["fails_before_fix"] is True
    assert automated["gaps"] == ["performance benchmark missing"]
    assert summary["proven_facts"]["candidate_artifacts"] == [
        {
            "candidate_id": "candidate-a",
            "parent_candidate_id": "candidate-base",
            "files": ["a.py"],
            "patch_digest": "sha256:patch",
            "final_artifact_ref": "source-manifest:sha256:state",
            "final_commit": "abc123",
            "revision_evidence_refs": ["finding:f1"],
        }
    ]
    assert summary["model_claims"][0]["validation_id"] == "self-report"
    assert "attempt attempt-2 has no Langfuse outcome" in summary["missing_evidence"]
    assert (
        "attempt attempt-1 produced candidate candidate-a with no evaluation record"
        not in summary["missing_evidence"]
    )
    assert "expected model haiku-4.5 has no invocation" in summary["missing_evidence"]
    assert summary["human_actions"] == ["provider evidence changes"]
    assert summary["truth_status"] == "incomplete"


def test_completed_candidate_with_only_model_claim_remains_unevaluated() -> None:
    summary = rs.build(
        {
            "run_uid": "run-2",
            "plan": {"objective": "Prove evaluation integrity", "rounds": {"maximum": 1}},
            "attempts": [
                {
                    "attempt_id": "attempt-unreviewed",
                    "requested_model": "gpt-5.6-luna",
                    "state": "completed",
                    "states": ["connecting", "gateway_accepted", "completed"],
                    "telemetry_state": "telemetry_verified",
                    "candidate_id": "candidate-unreviewed",
                }
            ],
            "roster": [],
            "latest_decision": {},
            "candidates": [{"candidate_id": "candidate-unreviewed", "files": ["a.py"]}],
            "validations": [
                {
                    "validation_id": "self-report",
                    "candidate_id": "candidate-unreviewed",
                    "source_kind": "model_self_report",
                    "status": "passed",
                }
            ],
            "requirements": [],
            "dispositions": [],
            "evidence_health": {},
        }
    )
    assert summary["truth_status"] == "incomplete"
    assert (
        "attempt attempt-unreviewed produced candidate candidate-unreviewed with no evaluation record"
        in summary["missing_evidence"]
    )
    assert (
        "candidate candidate-unreviewed has no validation evidence"
        in summary["missing_evidence"]
    )


def test_stopped_run_fails_truth_gate_for_unreconciled_roster_outcomes() -> None:
    summary = rs.build(
        {
            "run_uid": "run-3",
            "plan": {"objective": "Reconcile every expected model", "rounds": {"maximum": 1}},
            "attempts": [],
            "roster": [
                {
                    "model": "gpt-5.6-luna",
                    "state": "invoked",
                    "terminal_disposition": None,
                },
                {
                    "model": "haiku-4.5",
                    "state": "invoked",
                    "terminal_disposition": "completed_without_gateway_attempt",
                },
                {
                    "model": "grok-4.5",
                    "state": "telemetry_reconciled",
                    "terminal_disposition": "completed_not_evaluated_telemetry_missing",
                },
            ],
            "latest_decision": {"decision": "stop", "reason": "orchestration_error"},
            "candidates": [],
            "validations": [],
            "requirements": [],
            "dispositions": [],
            "evidence_health": {},
        }
    )

    assert summary["truth_status"] == "incomplete"
    assert "expected model gpt-5.6-luna has no terminal disposition" in summary["missing_evidence"]
    assert (
        "expected model haiku-4.5 completed without LiteLLM evidence"
        in summary["missing_evidence"]
    )
    assert (
        "expected model grok-4.5 ended as completed_not_evaluated_telemetry_missing"
        in summary["missing_evidence"]
    )
