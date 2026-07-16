"""Tests for the candidate-bound spec-conformance contract (ADR-0005).

The centrepiece is :class:`TestTheGrokFixture` — the 2026-07-16 failure replayed as a test. The
reviewer marked a requirement ``[met]`` while citing a real, resolvable line range and a real test
that asserted different fields, and the binding it claimed was structurally ``None``. These tests pin
both what the gate catches AND, explicitly, what it cannot: a false ``met`` that both assessors agree
on passes every mechanical check here. That limit is asserted, not hidden, so nobody later mistakes
this gate for proof.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import conformance as cf  # noqa: E402
import handoff_core as hc  # noqa: E402

_C1 = ("C1", "RiskPlan binds decision_generation to a non-default value")
_C2 = ("C2", "PortfolioProjector fails closed on an unpriced holding")


def _slice(tmp_path, constraints=(_C1, _C2)):
    collab = str(tmp_path / "c")
    hc.create(
        collab,
        to="builder",
        from_="reviewer",
        title="PT-6",
        body="build it",
        constraints=list(constraints) or None,
    )
    src = Path(collab) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "planner.py").write_text("\n".join(f"line {i}" for i in range(1, 120)), encoding="utf-8")
    tests = Path(collab) / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    # A REAL test file: cited test pointers are validated, so a fixture citing a fake one would fail
    # for the wrong reason (and did, when pointer validation was added).
    (tests / "t.py").write_text("\n".join(f"# t{i}" for i in range(1, 90)), encoding="utf-8")
    return collab


def _report(contract, statuses, sources=None):
    sources = sources or {}
    return json.dumps(
        {
            "contract_digest": contract.digest,
            "requirements": [
                {
                    "id": rid,
                    "status": statuses[rid],
                    "source": sources.get(rid, "src/planner.py:5"),
                    "test": "tests/t.py:65",
                    "evidence": "read it",
                }
                for rid in contract.ids
            ],
        }
    )


class TestCompileContract:
    def test_reuses_declared_constraints_and_digests_them(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        assert c.ids == ("C1", "C2")
        assert dict(c.requirements)["C1"] == _C1[1]
        assert c.digest.startswith("conformance:")

    def test_a_handoff_with_no_typed_constraints_is_refused(self, tmp_path):
        # An autonomous close claims "the spec was met". With no declared requirements that claim is
        # unfalsifiable, so it must fail before any model work rather than close vacuously.
        collab = _slice(tmp_path, constraints=())
        with pytest.raises(cc.CollabError, match="declares no typed constraints"):
            cf.compile_contract(collab, "001")

    def test_digest_changes_when_a_requirement_changes(self, tmp_path):
        a = cf.compile_contract(_slice(tmp_path / "a"), "001")
        b = cf.compile_contract(_slice(tmp_path / "b", constraints=(_C1, ("C2", "different text"))), "001")
        assert a.digest != b.digest  # changed constraints cannot reuse prior evidence

    def test_digest_changes_when_the_protocol_changes(self, tmp_path):
        collab = _slice(tmp_path)
        a = cf.compile_contract(collab, "001")
        b = cf.compile_contract(collab, "001", protocol_revision="spec-conformance-v99")
        assert a.digest != b.digest


class TestTheGrokFixture:
    """2026-07-16, replayed. The exact shape of the miss that motivated this gate."""

    def test_both_assessors_agreeing_on_a_false_met_STILL_PASSES(self, tmp_path):
        # THE HONEST LIMIT, asserted so it cannot be forgotten. grok cited planner.py:102-114 (real,
        # resolvable) and test_planner.py:65-66 (a real test asserting OTHER fields) for a binding
        # that was structurally None. Every mechanical check in this module passes that record.
        # Shape and resolvability cannot detect a false claim; only the second assessor disagreeing
        # can. This gate buys independent agreement, NOT proof — a requirement expressible as a type
        # or a test should be one, because verify.py cannot be talked out of a verdict.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        lie = _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:102-114"})
        r = cf.reconcile(c, lie, lie, source_base=collab)
        assert r["satisfied"] is True

    def test_an_independent_verifier_that_disagrees_refuses_the_close(self, tmp_path):
        # The actual defense: unknown must never close.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(
            c,
            _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:102-114"}),
            _report(c, {"C1": "missing", "C2": "met"}),
            source_base=collab,
        )
        assert r["satisfied"] is False
        assert r["incomplete"]["reason"] == "disagreement"

    def test_a_confirmed_missing_becomes_a_blocker_carrying_the_requirement(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(
            c,
            _report(c, {"C1": "missing", "C2": "met"}),
            _report(c, {"C1": "missing", "C2": "met"}),
            source_base=collab,
        )
        assert r["satisfied"] is False
        assert [b["id"] for b in r["blockers"]] == ["conformance:C1"]
        assert _C1[1] in r["blockers"][0]["description"]  # the builder gets the exact unmet item


class TestBothReportsAreValidated:
    """Regressions for a gap found in review: only the ASSESSOR's pointers were ever checked.

    The contract this gate advertises is "two independent, resolvable citations". Validating one side
    bought the words without the property — a verifier could cite a path outside the repo entirely and
    still close.
    """

    def test_verifier_source_outside_the_repo_is_refused(self, tmp_path):
        # The reproduction from review: verifier source '../outside.py:1' previously returned
        # satisfied=True, incomplete=None.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(
            c,
            _report(c, {"C1": "met", "C2": "met"}),
            _report(c, {"C1": "met", "C2": "met"}, {"C1": "../outside.py:1"}),
            source_base=collab,
        )
        assert r["satisfied"] is False
        assert r["incomplete"]["reason"] == "evidence"
        assert "verifier source" in r["incomplete"]["detail"]

    def test_verifier_source_that_does_not_exist_is_refused(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(
            c,
            _report(c, {"C1": "met", "C2": "met"}),
            _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:9999"}),
            source_base=collab,
        )
        assert r["satisfied"] is False and r["incomplete"]["reason"] == "evidence"

    def test_a_fabricated_test_pointer_is_refused(self, tmp_path):
        # A pointer offered as corroboration and never checked is worse than no pointer.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        bad = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [
                    {
                        "id": rid,
                        "status": "met",
                        "source": "src/planner.py:5",
                        "test": "tests/nope.py:9999",
                        "evidence": "e",
                    }
                    for rid in c.ids
                ],
            }
        )
        r = cf.reconcile(c, bad, bad, source_base=collab)
        assert r["satisfied"] is False
        assert r["incomplete"]["reason"] == "evidence" and "test" in r["incomplete"]["detail"]

    def test_a_null_test_is_allowed_but_a_bad_one_is_not(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        ok = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [
                    {"id": rid, "status": "met", "source": "src/planner.py:5", "test": None, "evidence": "e"}
                    for rid in c.ids
                ],
            }
        )
        r = cf.reconcile(c, ok, ok, source_base=collab)
        assert r["satisfied"] is True  # "no test covers this" is honest, not a refusal

    def test_both_evidence_records_are_retained(self, tmp_path):
        # Keeping only the assessor's record would make the verifier's agreement unauditable.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(
            c,
            _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:5"}),
            _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:9"}),
            source_base=collab,
        )
        assert r["satisfied"] is True
        c1 = next(x for x in r["results"] if x["id"] == "C1")
        assert c1["assessor"]["source_resolved"] == "src/planner.py:5-5"
        assert c1["verifier"]["source_resolved"] == "src/planner.py:9-9"


class TestEvidenceIsRefusedUnlessItResolves:
    def test_met_without_a_resolvable_source_is_incomplete(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        rep = _report(c, {"C1": "met", "C2": "met"}, {"C1": "src/planner.py:9999"})
        r = cf.reconcile(c, rep, rep, source_base=collab)
        assert r["satisfied"] is False and r["incomplete"]["reason"] == "evidence"

    def test_met_with_no_source_at_all_is_incomplete(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        rep = _report(c, {"C1": "met", "C2": "met"}, {"C1": None})
        r = cf.reconcile(c, rep, rep, source_base=collab)
        assert r["satisfied"] is False and r["incomplete"]["reason"] == "evidence"

    @pytest.mark.parametrize(
        "pointer",
        [
            "../../../etc/passwd:1",  # traversal
            "C:/Windows/system32/x:1",  # absolute
            "src/planner.py",  # no line
            "src/nope.py:1",  # no such file
            "src/planner.py:0",  # bad line
        ],
    )
    def test_pointer_refusals(self, tmp_path, pointer):
        collab = _slice(tmp_path)
        ok, _detail = cf.resolve_pointer(pointer, source_base=collab)
        assert ok is False

    def test_a_real_pointer_resolves(self, tmp_path):
        collab = _slice(tmp_path)
        ok, detail = cf.resolve_pointer("src/planner.py:5-9", source_base=collab)
        assert ok is True and detail == "src/planner.py:5-9"


class TestStrictReportParsing:
    def test_stale_contract_digest_is_refused(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        stale = json.dumps(
            {"contract_digest": "conformance:stale", "requirements": [{"id": "C1", "status": "met"}]}
        )
        r = cf.reconcile(c, stale, _report(c, {"C1": "met", "C2": "met"}), source_base=collab)
        assert r["satisfied"] is False and r["incomplete"]["reason"] == "assessor"

    def test_missing_an_id_is_incomplete_not_a_pass(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        partial = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [{"id": "C1", "status": "met", "source": "src/planner.py:5"}],
            }
        )
        r = cf.reconcile(c, partial, partial, source_base=collab)
        assert r["satisfied"] is False and "missing=['C2']" in r["incomplete"]["detail"]

    def test_an_unexpected_id_is_incomplete(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        extra = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [
                    {"id": rid, "status": "met", "source": "src/planner.py:5"} for rid in ("C1", "C2", "C9")
                ],
            }
        )
        r = cf.reconcile(c, extra, extra, source_base=collab)
        assert r["satisfied"] is False and "unexpected=['C9']" in r["incomplete"]["detail"]

    def test_duplicate_records_are_incomplete(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        dup = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [
                    {"id": "C1", "status": "met", "source": "src/planner.py:5"},
                    {"id": "C1", "status": "missing"},
                    {"id": "C2", "status": "met", "source": "src/planner.py:5"},
                ],
            }
        )
        r = cf.reconcile(c, dup, dup, source_base=collab)
        assert r["satisfied"] is False and "duplicate" in r["incomplete"]["detail"]

    @pytest.mark.parametrize("raw", ["", "   ", "no json here", "{not json}", "[]", '{"requirements": []}'])
    def test_malformed_output_is_incomplete_never_a_pass(self, tmp_path, raw):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        r = cf.reconcile(c, raw, _report(c, {"C1": "met", "C2": "met"}), source_base=collab)
        assert r["satisfied"] is False and r["incomplete"] is not None

    def test_invalid_status_is_refused(self, tmp_path):
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        bad = json.dumps(
            {
                "contract_digest": c.digest,
                "requirements": [{"id": rid, "status": "probably-fine"} for rid in ("C1", "C2")],
            }
        )
        r = cf.reconcile(c, bad, bad, source_base=collab)
        assert r["satisfied"] is False

    def test_a_fenced_json_block_is_accepted(self, tmp_path):
        # Models wrap JSON in ``` fences constantly; refusing that would be brittle, not strict.
        collab = _slice(tmp_path)
        c = cf.compile_contract(collab, "001")
        fenced = f"Here you go:\n```json\n{_report(c, {'C1': 'met', 'C2': 'met'})}\n```\n"
        r = cf.reconcile(c, fenced, _report(c, {"C1": "met", "C2": "met"}), source_base=collab)
        assert r["satisfied"] is True


class TestPrompt:
    def test_prompt_carries_every_requirement_and_the_digest(self, tmp_path):
        c = cf.compile_contract(_slice(tmp_path), "001")
        p = cf.build_prompt(c, role="assessor")
        assert c.digest in p and _C1[1] in p and _C2[1] in p

    def test_verifier_prompt_is_independent_and_defaults_to_missing(self, tmp_path):
        c = cf.compile_contract(_slice(tmp_path), "001")
        p = cf.build_prompt(c, role="verifier")
        assert "own conclusion" in p and "Default to 'missing'" in p

    @pytest.mark.parametrize("role", ["Verifier", "breaker", "", "assessor "])
    def test_an_unknown_role_is_refused(self, tmp_path, role):
        # Treating every non-'assessor' string as the verifier would let a typo hand BOTH seats the
        # assessor stance — collapsing independence into two identical opinions that still "agree".
        c = cf.compile_contract(_slice(tmp_path), "001")
        with pytest.raises(cc.CollabError, match="unknown conformance role"):
            cf.build_prompt(c, role=role)
