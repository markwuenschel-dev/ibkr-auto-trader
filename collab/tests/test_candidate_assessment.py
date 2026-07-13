"""Tests for candidate_assessment — the deep assessment-policy module (ADR-0003 D4, Phase 4).

Pins candidate identity (extends run_budget), the merge table (evidence-backed blocks; lane
refutation cannot erase a reviewer blocker; unsupported concern is advisory), cross-candidate
finding history, malformed-review pausing, immutable completed assessments, cache reuse, and a retry
that reuses evidence without the builder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import candidate_assessment as ca  # noqa: E402
import collab_common as cc  # noqa: E402


def _cand(tmp_hid="030", *, rubric="r1", src=None, roots=("src",)):
    return ca.Candidate.compute(
        tmp_hid, source_manifest=src or {"a.py": "h1"}, source_roots=list(roots),
        test_command="pytest", lane_config={}, contract_revision="c1",
        assessment_plan_revision="p1", reviewer_rubric=rubric, seat_profile_fingerprint="seat:abc",
    )


def _finding(fp="f1", *, severity="blocking", category="correctness", evidence="crashes on x=0"):
    return {"fingerprint": fp, "source": "reviewer", "severity": severity,
            "category": category, "evidence": evidence, "remediation": "guard x"}


def _report(blocking=(), advisory=(), edited=False):
    return {"requirement_coverage": {"r1": "met"}, "blocking_findings": list(blocking),
            "advisory_findings": list(advisory), "edited_code": edited}


def _rep(cand, blocking=(), advisory=()):
    return ca.ReviewerReport.parse(_report(blocking, advisory), candidate_id=cand.candidate_id)


_CLEAN = {"confirmed": [], "refuted": [], "ran": True}


class TestCandidateIdentity:
    def test_extends_run_budget_rubric_change_mints_new_id(self):
        a = _cand(rubric="r1")
        b = _cand(rubric="r2")
        assert a.candidate_id != b.candidate_id

    def test_source_change_mints_new_id(self):
        a = _cand(src={"a.py": "h1"})
        b = _cand(src={"a.py": "h2"})
        assert a.candidate_id != b.candidate_id

    def test_identical_inputs_stable(self):
        assert _cand().candidate_id == _cand().candidate_id


class TestReviewerReportParse:
    def test_edited_code_rejected(self):
        with pytest.raises(ca.MalformedReview, match="edited_code"):
            ca.ReviewerReport.parse(_report(edited=True), candidate_id="cand:x")

    def test_invalid_json_rejected(self):
        with pytest.raises(ca.MalformedReview):
            ca.ReviewerReport.parse("{not json", candidate_id="cand:x")

    def test_non_object_rejected(self):
        with pytest.raises(ca.MalformedReview):
            ca.ReviewerReport.parse("[1,2,3]", candidate_id="cand:x")


class TestMergeTable:
    def test_evidence_backed_blocker_requires_repair(self, tmp_path):
        cand = _cand()
        rep = _rep(cand, blocking=[_finding("f1", evidence="crashes on empty input")])
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=rep, lane_ledger=_CLEAN)
        assert a.outcome == ca.REPAIR_REQUIRED
        assert any(f.fingerprint == "f1" for f in a.unresolved_findings)

    def test_blocker_without_evidence_is_advisory(self, tmp_path):
        cand = _cand()
        rep = _rep(cand, blocking=[_finding("f1", evidence="")])  # claims blocking, no evidence
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=rep, lane_ledger=_CLEAN)
        assert a.outcome == ca.APPROVED  # unsupported concern never blocks

    def test_clean_run_approves(self, tmp_path):
        cand = _cand()
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=_CLEAN)
        assert a.outcome == ca.APPROVED

    def test_lane_confirmed_blocks(self, tmp_path):
        cand = _cand()
        ledger = {"confirmed": [{"fingerprint": "L1", "lane": "safety", "evidence": "asserts fire",
                                 "category": "safety"}], "refuted": []}
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=ledger)
        assert a.outcome == ca.REPAIR_REQUIRED

    def test_lane_refutation_does_not_erase_reviewer_blocker(self, tmp_path):
        cand = _cand()
        rep = _rep(cand, blocking=[_finding("f1", evidence="real defect at a.py:10")])
        ledger = {"confirmed": [], "refuted": [{"fingerprint": "other", "lane": "x"}]}
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=rep, lane_ledger=ledger)
        assert a.outcome == ca.REPAIR_REQUIRED  # the reviewer blocker still stands


class TestInfraAndIncomplete:
    def test_tool_error_is_infrastructure_blocked(self, tmp_path):
        cand = _cand()
        ledger = {"tool_error": {"lane": "safety", "cmd": "openai-repo-seat ...", "exit": 2,
                                 "stderr": "unrecognized arguments"}}
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=ledger)
        assert a.outcome == ca.INFRASTRUCTURE_BLOCKED
        assert a.cause["exit"] == 2
        # partial evidence persisted for a later retry
        assert ca.load_partial(str(tmp_path), "030", cand.candidate_id) is not None

    def test_malformed_review_is_verification_incomplete(self, tmp_path):
        cand = _cand()
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=None, lane_ledger=_CLEAN)
        assert a.outcome == ca.VERIFICATION_INCOMPLETE

    def test_findings_overflow_is_verification_incomplete(self, tmp_path):
        cand = _cand()
        ledger = {"confirmed": [], "overflow": 3, "unverified": ["x", "y", "z"]}
        a = ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=ledger)
        assert a.outcome == ca.VERIFICATION_INCOMPLETE
        assert a.cause["overflow"] == 3


class TestFindingHistory:
    def test_unresolved_carries_across_candidates(self, tmp_path):
        c1 = _cand(src={"a.py": "h1"})
        rep1 = _rep(c1, blocking=[_finding("f1", evidence="bug at a.py:5")])
        a1 = ca.complete(str(tmp_path), "030", c1, reviewer_report=rep1, lane_ledger=_CLEAN)
        assert a1.outcome == ca.REPAIR_REQUIRED
        # A new candidate that no longer triggers f1 marks it fixed.
        c2 = _cand(src={"a.py": "h2"})
        a2 = ca.complete(str(tmp_path), "030", c2, reviewer_report=_rep(c2), lane_ledger=_CLEAN)
        assert a2.outcome == ca.APPROVED
        led = ca.FindingLedger.load(str(tmp_path), "030")
        f1 = next(f for f in led.all() if f.fingerprint == "f1")
        assert f1.status == ca.FIXED

    def test_corrupt_ledger_refuses(self, tmp_path):
        c1 = _cand()
        ca.complete(str(tmp_path), "030", c1, reviewer_report=_rep(c1), lane_ledger=_CLEAN)
        fp = ca._findings_path(str(tmp_path), "030")
        fp.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(cc.CollabError):
            ca.FindingLedger.load(str(tmp_path), "030")


class TestCacheAndImmutability:
    def test_prepare_returns_cached_completed_assessment(self, tmp_path):
        cand = _cand()
        ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=_CLEAN)
        prep = ca.prepare(str(tmp_path), "030", candidate=cand)
        assert prep["cached"] is not None
        assert prep["cached"].outcome == ca.APPROVED

    def test_prepare_no_cache_when_absent(self, tmp_path):
        prep = ca.prepare(str(tmp_path), "030", candidate=_cand())
        assert prep["cached"] is None

    def test_immutable_assessment_refuses_conflicting_overwrite(self, tmp_path):
        cand = _cand()
        approved = ca.CandidateAssessment("030", cand.candidate_id, ca.APPROVED, (), ())
        ca.save_assessment(str(tmp_path), approved)
        repaired = ca.CandidateAssessment("030", cand.candidate_id, ca.REPAIR_REQUIRED, (), ())
        with pytest.raises(ca.AssessmentImmutableViolation):
            ca.save_assessment(str(tmp_path), repaired)


class TestRetry:
    def test_retry_reuses_reviewer_evidence_and_completes(self, tmp_path):
        cand = _cand()
        # First pass: reviewer OK but lanes crashed -> infrastructure_blocked, partial saved.
        ledger = {"tool_error": {"lane": "safety", "cmd": "x", "exit": 2, "stderr": "boom"}}
        first = ca.complete(str(tmp_path), "030", cand, reviewer_report=_rep(cand), lane_ledger=ledger)
        assert first.outcome == ca.INFRASTRUCTURE_BLOCKED
        # Retry supplying only the now-fixed lanes; the reviewer report is reused from partial.
        second = ca.retry(str(tmp_path), "030", cand, lane_ledger=_CLEAN)
        assert second.outcome == ca.APPROVED  # reused reviewer evidence, no builder call
