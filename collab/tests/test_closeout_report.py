"""Tests for closeout_report.py — the read-only autonomous-closeout evidence summary (slice 7).

collect() reads the verification ledger + recomputes the done-contract verdict + reads state/events and
returns an auditable summary. It transitions nothing. These tests pin: both render formats, blocked-verdict
reflection, missing-ledger tolerance, autonomous_done event detection, and (critically) read-only-ness.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import closeout_report as cr  # noqa: E402
import gate_runner as gr  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402
import lanes  # noqa: E402


def _preflight(base, seat="reviewer"):
    return {
        "seat": seat,
        "repo_access": True,
        "repo_root": str(base),
        "commands": {
            "pwd": {"exit_code": 0, "stdout_tail": str(base)},
            "git_rev_parse": {"exit_code": 0, "stdout_tail": str(base)},
            "git_status_short": {"exit_code": 0, "stdout_tail": ""},
            "pytest_collect_only": {"exit_code": 0, "stdout_tail": "1 test collected"},
        },
        "inspected_files": ["src/m.py"],
    }


def _setup(tmp_path, *, drift=False, tests_passed=True):
    """A CLAIMED handoff + a satisfied verification ledger (unless drifted). Left claimed so the recomputed
    verdict is meaningful (condition 10 wants pending|claimed)."""
    collab = str(tmp_path / "c")
    hc.create(
        collab, to="reviewer", from_="builder", title="Add closeout-report command", body="please review"
    )
    hc.claim(collab, "001")
    base = Path(collab)
    (base / "src").mkdir(parents=True, exist_ok=True)
    (base / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    ledger = {
        "hid": "001",
        "generated_ts": ap._now_utc(),
        "guardrails": [],
        "builder_seat": "builder",
        "reviewer_seat": "reviewer",
        "source_base": str(base),
        "source_manifest": gr.source_manifest(["src/*.py"], base),
        "tests": {"passed": tests_passed, "run_id": "t"},
        "reviewer_preflight": _preflight(base),
        "lanes": [],
        "blockers": [],
        "accepted_residuals": [],
    }
    lanes.write_ledger(collab, "001", ledger)
    if drift:
        (base / "src" / "m.py").write_text("x = 2\n", encoding="utf-8")  # drift after manifest
    return collab


class TestCollect:
    def test_collects_summary_on_satisfied_evidence(self, tmp_path):
        s = cr.collect(_setup(tmp_path), "001")
        assert s["done_contract"]["satisfied"] is True
        assert len(s["done_contract"]["conditions"]) == 11
        assert s["reviewer_preflight"]["present"] is True
        assert s["source_manifest"]["file_count"] == 1
        assert s["final_state"] == "claimed"

    def test_condition_table_reflects_blocked_verdict(self, tmp_path):
        s = cr.collect(_setup(tmp_path, drift=True), "001")
        assert s["done_contract"]["satisfied"] is False
        failed = {c["name"] for c in s["done_contract"]["conditions"] if c["status"] != "pass"}
        assert "source==tested" in failed

    def test_missing_ledger_is_handled(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        s = cr.collect(collab, "001")
        assert s["ledger_present"] is False
        assert s["done_contract"]["satisfied"] is False  # no crash, no fake green

    def test_detects_autonomous_done_event(self, tmp_path):
        collab = _setup(tmp_path)
        hc.done(collab, "001")  # advance to done (as the driver would)
        he.on_autonomous_done(
            str(Path(collab) / "logs" / "events.jsonl"),
            "rid",
            "001",
            span_id="001:signoff",
            parent_span_id=None,
            reviewer="reviewer",
            contract_hash="deadbeef",
        )
        s = cr.collect(collab, "001")
        assert s["autonomous_done_event"] is True
        assert s["final_state"] == "done"
        assert s["closed_autonomously"] is True


class TestRender:
    def test_markdown_renders(self, tmp_path):
        md = cr.render_markdown(cr.collect(_setup(tmp_path), "001"))
        assert "# Closeout report — 001" in md
        assert "Done-contract conditions" in md
        assert "reviewer-repo-preflight" in md  # a condition-table row

    def test_json_is_valid_and_stable(self, tmp_path):
        collab = _setup(tmp_path)
        a = cr.render_json(cr.collect(collab, "001"))
        b = cr.render_json(cr.collect(collab, "001"))
        assert json.loads(a)["handoff_id"] == "001"
        assert a == b  # deterministic: sorted keys + stable verdict hash


class TestReadOnly:
    def test_report_does_not_transition_state(self, tmp_path):
        collab = _setup(tmp_path)
        before = hc.state_of(collab, "001")
        cr.collect(collab, "001")
        cr.render(collab, "001", "markdown")
        cr.render(collab, "001", "json")
        assert hc.state_of(collab, "001") == before == "claimed"  # untouched


class TestCli:
    def test_cli_markdown_exit_ok(self, tmp_path, capsys):
        rc = cr.main([_setup(tmp_path), "001", "--format", "markdown"])
        assert rc == 0 and "Closeout report" in capsys.readouterr().out

    def test_cli_json_exit_ok(self, tmp_path, capsys):
        rc = cr.main([_setup(tmp_path), "001", "--format", "json"])
        assert rc == 0 and json.loads(capsys.readouterr().out)["handoff_id"] == "001"

    def test_cli_unknown_handoff_exit4(self, tmp_path):
        collab = str(tmp_path / "empty")
        Path(collab).mkdir()
        assert cr.main([collab, "999"]) == 4
