"""Typed validation and test-quality evidence contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import quality_evidence as qe  # noqa: E402


def test_automated_validation_retains_baseline_delta_dimensions_and_test_quality(tmp_path: Path) -> None:
    event = qe.record_validation(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:abc",
        validation_id="pytest:focused",
        timestamp="2026-07-22T12:00:00Z",
        source_kind="automated_check",
        status="passed",
        producer="pytest",
        producer_version="9.0",
        artifact_ref="verification/focused.json",
        dimensions={"correctness": "passed", "regression": "passed"},
        baseline_delta={"before": "failed", "after": "passed", "changed": True},
        uncertainty="none observed within the focused scope",
        gaps=("browser behavior not exercised",),
        proves_changed_behavior=True,
        fails_before_fix=True,
        exercises_degraded_modes=False,
        avoids_over_mocking=True,
        detects_negative_variants=True,
    )
    assert event["source_kind"] == "automated_check"
    assert event["test_quality"]["fails_before_fix"] is True
    assert event["baseline_delta"]["changed"] is True
    assert qe.read_validations(tmp_path, run_uid="run-1") == [event]


@pytest.mark.parametrize(
    "source_kind",
    ["evaluator_judgment", "model_self_report", "human_decision"],
)
def test_non_automated_sources_remain_distinct_and_cannot_claim_test_quality(
    tmp_path: Path, source_kind: str
) -> None:
    event = qe.record_validation(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:abc",
        validation_id=f"{source_kind}:1",
        timestamp="2026-07-22T12:00:00Z",
        source_kind=source_kind,
        status="warning",
        producer=source_kind,
        producer_version="v1",
        artifact_ref=f"evidence:{source_kind}",
        dimensions={"maintainability": "uncertain"},
        uncertainty="subjective or non-executable evidence",
        gaps=("not an automated oracle",),
    )
    assert event["test_quality"] is None


def test_model_self_report_cannot_be_promoted_to_acceptance_oracle(tmp_path: Path) -> None:
    with pytest.raises(qe.QualityEvidenceError, match="oracle"):
        qe.record_validation(
            tmp_path,
            run_uid="run-1",
            candidate_id="cand:abc",
            validation_id="self:1",
            timestamp="2026-07-22T12:00:00Z",
            source_kind="model_self_report",
            status="passed",
            producer="builder-model",
            producer_version="v1",
            artifact_ref="reply:1",
            dimensions={"correctness": "passed"},
            uncertainty="self report",
            is_acceptance_oracle=True,
        )
