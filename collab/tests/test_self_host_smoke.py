"""Tests for self_host_smoke.py — the self-hosting production harness (slice 7).

Each test runs the REAL closeout pipeline (real git preflight, real pytest subprocess on a seeded slice,
real adversarial lanes with scripted agents, the pure done-contract) against a disposable workspace. The
clean run must reach ``done`` with a full evidence bundle; every ``--inject`` negative must be BLOCKED and
leave the handoff ``claimed``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import self_host_smoke as sh  # noqa: E402


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    ws = tmp_path_factory.mktemp("clean")
    return sh.run_smoke(inject="clean", workspace=str(ws))


class TestCleanRun:
    def test_creates_disposable_collab(self, clean):
        assert Path(clean["collab"]).exists()
        assert clean["collab"].startswith(
            clean["workspace"]
        )  # under the disposable workspace, not real state

    def test_captures_repo_preflight(self, clean):
        pre = lanes.read_ledger(clean["collab"], clean["handoff_id"])["reviewer_preflight"]
        assert pre and pre["repo_access"] is True and pre["seat"] == "reviewer"
        assert pre["commands"]["git_rev_parse"]["exit_code"] == 0
        assert pre["commands"]["pytest_collect_only"]["exit_code"] == 0
        assert pre["inspected_files"]

    def test_writes_source_manifest(self, clean):
        man = lanes.read_ledger(clean["collab"], clean["handoff_id"])["source_manifest"]
        assert man and "closeout_report.py" in man

    def test_runs_required_lanes(self, clean):
        # v2 batches contracts into breaker->verifier PAIRS (ADR-0004 D2) instead of fanning out one lane
        # per contract: the same five baseline contracts now ride one pair, and `data-integrity` (a
        # high-risk guardrail) adds exactly one composite pair.
        led = lanes.read_ledger(clean["collab"], clean["handoff_id"])
        assert [x["pass"] for x in led["lanes"]] == ["baseline", "high-risk-diverse"]
        assert all(x["ran"] for x in led["lanes"])
        assert len(led["lanes"][0]["contracts"]) == 5  # the five contracts the legacy fan-out ran
        assert led["lanes"][1]["composite"] is True
        assert led["verification_plan_digest"].startswith("plan:")  # the plan is BOUND to this evidence

    def test_records_test_evidence(self, clean):
        assert lanes.read_ledger(clean["collab"], clean["handoff_id"])["tests"]["passed"] is True

    def test_writes_done_contract_verdict(self, clean):
        dc = json.loads((Path(clean["bundle_dir"]) / "done_contract.json").read_text("utf-8"))
        assert dc["satisfied"] is True and len(dc["conditions"]) == 11

    def test_reaches_done_on_clean_evidence(self, clean):
        assert clean["reached_done"] is True
        assert hc.state_of(clean["collab"], clean["handoff_id"]) == "done"
        summary = json.loads((Path(clean["bundle_dir"]) / "summary.json").read_text("utf-8"))
        assert summary["autonomous_done_event"] is True and summary["closed_autonomously"] is True

    def test_summary_is_auditable(self, clean):
        d = Path(clean["bundle_dir"])
        for name in sh.BUNDLE_FILES:
            assert (d / name).exists(), f"missing bundle file: {name}"
        summary = json.loads((d / "summary.json").read_text("utf-8"))
        assert summary["handoff_id"] == clean["handoff_id"]
        assert summary["seats"]["builder"] == "builder" and summary["seats"]["reviewer"] == "reviewer"
        assert len(summary["done_contract"]["conditions"]) == 11


def _blocked(tmp_path_factory, scenario):
    ws = tmp_path_factory.mktemp(scenario.replace("-", "_"))
    return sh.run_smoke(inject=scenario, workspace=str(ws))


class TestNegativeProofs:
    def test_blocks_self_approval(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "self-approval")
        assert r["reached_done"] is False
        assert hc.state_of(r["collab"], r["handoff_id"]) == "claimed"
        assert "independent-approver" in r["unmet"]

    def test_blocks_source_drift(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "source-drift")
        assert r["reached_done"] is False and "source==tested" in r["unmet"]

    def test_blocks_test_failure(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "test-failure")
        assert r["reached_done"] is False and "blocker-regressions" in r["unmet"]

    def test_blocks_confirmed_finding(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "confirmed-finding")
        assert r["reached_done"] is False and "blockers-fixed" in r["unmet"]

    def test_blocks_missing_ledger(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "missing-ledger")
        assert r["reached_done"] is False and "builder-evidence" in r["unmet"]

    def test_blocks_missing_preflight(self, tmp_path_factory):
        r = _blocked(tmp_path_factory, "missing-preflight")
        assert r["reached_done"] is False and "reviewer-repo-preflight" in r["unmet"]


class TestCli:
    def test_cli_clean_exit_ok(self, tmp_path, capsys):
        rc = sh.main(["--workspace", str(tmp_path / "w"), "--format", "json"])
        assert rc == 0 and json.loads(capsys.readouterr().out)["reached_done"] is True

    def test_cli_injected_negative_exit_ok(self, tmp_path, capsys):
        # A blocked negative is the EXPECTED outcome for --inject -> exit 0.
        rc = sh.main(["--inject", "source-drift", "--workspace", str(tmp_path / "w"), "--format", "json"])
        assert rc == 0 and json.loads(capsys.readouterr().out)["reached_done"] is False
