"""Evidence-complete candidate acceptance/rejection/supersession records."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import candidate_disposition as cd  # noqa: E402


def _base() -> dict:
    return {
        "run_uid": "run-1",
        "candidate_id": "cand:abc",
        "handoff_id": "030",
        "timestamp": "2026-07-22T12:00:00Z",
        "executive_explanation": "Candidate failed the authoritative contract on source equality.",
        "evidence_refs": ("contract:abc",),
        "impact": "The candidate cannot be shipped autonomously.",
        "remediation": "Rebuild from the tested source and rerun the contract.",
        "retryable": True,
        "final": False,
        "retained_work": ("test fixture",),
        "alternatives": ("rebuild", "human review"),
        "weaknesses": ("source drift",),
        "unavailable_evidence": (),
        "confidence": "high",
        "disagreements": ({"reviewer": "accept", "oracle": "reject"},),
        "resolution": "authoritative oracle overruled provisional reviewer acceptance",
        "human_review_triggers": ("repeated source drift",),
        "decision_maker": "done-contract",
        "primary_reason": "contract_violation",
        "failed_checks": ("source equality",),
        "round_number": 2,
    }


def test_rejection_retains_explanation_remediation_disagreement_and_useful_work(tmp_path: Path) -> None:
    event = cd.record_disposition(tmp_path, disposition="rejected", **_base())
    assert event["disposition"] == "rejected"
    assert event["retained_work"] == ["test fixture"]
    assert event["disagreements"][0]["oracle"] == "reject"
    assert event["primary_reason"] == "contract_violation"
    assert event["failed_checks"] == ["source equality"]
    assert cd.read_dispositions(tmp_path, run_uid="run-1") == [event]


def test_acceptance_requires_complete_critical_requirements_and_named_oracle(tmp_path: Path) -> None:
    with pytest.raises(cd.CandidateDispositionError, match="requirements"):
        cd.record_disposition(
            tmp_path,
            disposition="accepted",
            requirements_evaluation={"eligible": False, "oracle_validation_id": "done-contract"},
            **_base(),
        )
    accepted = cd.record_disposition(
        tmp_path,
        disposition="accepted",
        requirements_evaluation={
            "eligible": True,
            "oracle_validation_id": "done-contract",
            "oracle_status": "passed",
        },
        **_base(),
    )
    assert accepted["acceptance_oracle"] == "done-contract"
    assert accepted["weaknesses"] == ["source drift"]  # warnings remain visible


def test_rejection_requires_a_typed_reason_category(tmp_path: Path) -> None:
    base = _base()
    base["primary_reason"] = "vibes"
    with pytest.raises(cd.CandidateDispositionError, match="rejection reason"):
        cd.record_disposition(tmp_path, disposition="rejected", **base)


def test_rejection_retains_multiple_typed_reason_categories(tmp_path: Path) -> None:
    event = cd.record_disposition(
        tmp_path,
        disposition="rejected",
        reason_categories=("contract_violation", "missing_runtime_proof", "telemetry_gap"),
        **_base(),
    )
    assert event["primary_reason"] == "contract_violation"
    assert event["reason_categories"] == [
        "contract_violation",
        "missing_runtime_proof",
        "telemetry_gap",
    ]

    with pytest.raises(cd.CandidateDispositionError, match="rejection reason"):
        cd.record_disposition(
            tmp_path / "invalid",
            disposition="rejected",
            reason_categories=("contract_violation", "vibes"),
            **_base(),
        )


def test_superseded_disposition_requires_and_retains_successor(tmp_path: Path) -> None:
    with pytest.raises(cd.CandidateDispositionError, match="successor"):
        cd.record_disposition(tmp_path, disposition="superseded", **_base())

    event = cd.record_disposition(
        tmp_path,
        disposition="superseded",
        superseded_by_candidate_id="cand:def",
        **_base(),
    )
    assert event["superseded_by_candidate_id"] == "cand:def"
