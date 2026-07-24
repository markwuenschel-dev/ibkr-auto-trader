"""Requirement coverage and acceptance-oracle contracts."""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import requirements_matrix as rm  # noqa: E402


def _record(tmp_path: Path, requirement_id: str, *, critical=True, status="met", source="automated_check"):
    return rm.record_requirement(
        tmp_path,
        run_uid="run-1",
        candidate_id="cand:abc",
        requirement_id=requirement_id,
        description=f"Requirement {requirement_id}",
        critical=critical,
        status=status,
        source_kind=source,
        evidence_refs=(f"validation:{requirement_id}",),
        producer="requirements-evaluator",
        producer_version="v1",
        timestamp="2026-07-22T12:00:00Z",
    )


def test_acceptance_requires_all_critical_requirements_and_named_passing_oracle(tmp_path: Path) -> None:
    records = [_record(tmp_path, "R-1"), _record(tmp_path, "R-2"), _record(tmp_path, "R-3", critical=False)]
    validations = [
        {
            "validation_id": "done-contract",
            "candidate_id": "cand:abc",
            "status": "passed",
            "source_kind": "automated_check",
            "is_acceptance_oracle": True,
        }
    ]
    result = rm.evaluate_acceptance(
        records,
        validations=validations,
        candidate_id="cand:abc",
        oracle_validation_id="done-contract",
    )
    assert result == {
        "eligible": True,
        "critical_total": 2,
        "critical_met": 2,
        "unmet": [],
        "oracle_validation_id": "done-contract",
        "oracle_status": "passed",
    }


def test_missing_critical_requirement_or_oracle_keeps_acceptance_ineligible(tmp_path: Path) -> None:
    records = [_record(tmp_path, "R-1"), _record(tmp_path, "R-2", status="missing")]
    result = rm.evaluate_acceptance(
        records,
        validations=[],
        candidate_id="cand:abc",
        oracle_validation_id="done-contract",
    )
    assert result["eligible"] is False
    assert result["unmet"] == ["R-2"]
    assert result["oracle_status"] == "missing"


def test_model_self_report_does_not_satisfy_critical_requirement(tmp_path: Path) -> None:
    record = _record(tmp_path, "R-1", source="model_self_report")
    matrix = rm.project([record])
    assert matrix[0]["effective_status"] == "unverified"
