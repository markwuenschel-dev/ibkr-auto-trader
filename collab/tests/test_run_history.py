"""Tests for the telemetry-history feature: the per-run archive + roll-up (``run_history.py``) and the
dashboard read/act surface that exposes it (``dashboard_core`` run-history functions + ``dashboard_web``
routes), plus the driver's run-start wipe / run-end archive and the live max-rounds knob.

Same harness posture as test_autopilot.py / test_dashboard.py: the agent backend is INJECTED (``runner=``)
so the whole loop runs with a FAKE agent — no real CLI, no network. These tests pin the durable-history
contract (a finished run is archived on EVERY exit path and is inspectable/comparable long after the live
feed rotates) and the safety guardrails around it (run_uid path-safety, max_rounds range validation).
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import collab_common as cc  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402
import run_history as rh  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "events_sample.jsonl"


def _cli(seat_names):
    return {s: {"backend": "cli", "cmd": [f"model-{s}"], "system": f"You are the {s}."} for s in seat_names}


# --------------------------------------------------------------------------- #
# build_summary — aggregation over a realistic archived event feed
# --------------------------------------------------------------------------- #


class TestBuildSummary:
    def test_aggregates_rounds_lanes_signoff_and_handoffs(self, tmp_path):
        # The fixture is a self-contained subset of a real archived run: round start+done events, lane
        # breaker/verdict/rollup events, a signoff_blocked, and a deliberately TORN final line. Every
        # expected value below is hand-computed against exactly those lines.
        collab = str(tmp_path / "c")
        summary = rh.build_summary(
            collab,
            "20260709T000017Z-4242",
            seats={"builder": "opus", "reviewer": "gpt"},
            started_ts="2026-07-09T00:00:17Z",
            pid=4242,
            max_rounds=6,
            watch=False,
            git_sha="deadbeef",
            events_path=str(_FIXTURE),
        )

        # calls == rounds_total == count of round DONE events (latency_ms present); 'start' variants excluded,
        # and the torn final line (a 4th would-be round) is skipped — proving torn lines never inflate counts.
        assert summary["rounds_total"] == 3
        assert summary["calls"] == 3
        # partitioned by role: builder took two turns (h027 + h028), reviewer one.
        assert summary["seat_calls"] == {"builder": 2, "reviewer": 1}
        # seat_latency_ms sums metrics.latency_ms per role (rounded to .1).
        assert summary["seat_latency_ms"] == {"builder": 205759.7, "reviewer": 8708.6}
        # lanes: only decision.action == "lane" rollup events count (breaker/verdict excluded).
        assert summary["lanes"]["confirmed"] == 1
        assert summary["lanes"]["refuted"] == 1
        assert summary["lanes"]["by_lane"] == {
            "bounded-autonomy": {"confirmed": 0, "refuted": 1},
            "path-pointer-safety": {"confirmed": 1, "refuted": 0},
        }
        # signoff: derived from the signoff_blocked event, "unmet:" prefixes stripped.
        assert summary["signoff"]["result"] == "blocked"
        assert summary["signoff"]["unmet"] == ["builder-evidence", "independent-approver", "lanes-ran"]
        # handoffs_touched: distinct hids in first-seen order.
        assert summary["handoffs_touched"] == ["027", "028"]
        # identity/context kwargs flow straight through.
        assert summary["run_uid"] == "20260709T000017Z-4242"
        assert summary["pid"] == 4242 and summary["git_sha"] == "deadbeef"
        assert summary["max_rounds"] == 6 and summary["watch"] is False
        assert summary["seats"] == {"builder": "opus", "reviewer": "gpt"}

    def test_signed_result_from_autonomous_done(self, tmp_path):
        # An autonomous_done event flips signoff.result to "signed" (unmet empty).
        collab = str(tmp_path / "c")
        log = Path(collab) / "logs" / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps(
                {
                    "stage": "autopilot.round",
                    "role": "reviewer",
                    "artifact": "handoff:001",
                    "metrics": {"latency_ms": 12.0},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "stage": "autopilot.autonomous_done",
                    "role": "reviewer",
                    "artifact": "handoff:001",
                    "decision": {"reason_codes": ["done:001"]},
                }
            )
            + "\n",
            "utf-8",
        )
        summary = rh.build_summary(collab, "uid-1", events_path=str(log))
        assert summary["signoff"] == {"result": "signed", "unmet": []}
        assert summary["rounds_total"] == 1

    def test_duration_ms_from_status_when_ended(self, tmp_path):
        # duration_ms is (ended - started) in ms, recovered from status.json when kwargs omit them.
        collab = str(tmp_path / "c")
        ap._write_status(collab, started_ts="2026-07-09T00:00:00Z", phase="done")
        # stamp a deterministic terminal timestamp
        sp = ap._status_path(collab)
        doc = json.loads(sp.read_text("utf-8"))
        doc["started_ts"] = "2026-07-09T00:00:00Z"
        doc["updated_ts"] = "2026-07-09T00:00:30Z"
        sp.write_text(json.dumps(doc), "utf-8")
        summary = rh.build_summary(collab, "uid-dur", events_path=str(_FIXTURE))
        assert summary["duration_ms"] == 30000
        assert summary["phase_final"] == "done"

    def test_torn_and_missing_feed_never_raise(self, tmp_path):
        # A missing feed yields empty aggregates, never an exception.
        collab = str(tmp_path / "c")
        summary = rh.build_summary(collab, "uid-empty", events_path=str(tmp_path / "nope.jsonl"))
        assert summary["rounds_total"] == 0 and summary["seat_calls"] == {}
        assert summary["signoff"] == {"result": "none", "unmet": []}
        assert summary["handoffs_touched"] == []


# --------------------------------------------------------------------------- #
# archive_run / prune
# --------------------------------------------------------------------------- #


class TestArchiveAndPrune:
    def test_archive_run_snapshots_events_status_and_rollup(self, tmp_path):
        collab = str(tmp_path / "c")
        log = Path(ap._log_default(collab))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(_FIXTURE.read_text("utf-8"), "utf-8")
        ap._write_status(collab, started_ts="2026-07-09T00:00:17Z", phase="capped", run_uid="uid-A")

        root = rh.archive_run(collab, "20260709T000017Z-4242")
        assert root is not None and root.is_dir()
        assert root.name == "20260709T000017Z-4242"
        assert (root / "events.jsonl").exists()
        assert (root / "status.json").exists()
        doc = json.loads((root / "run.json").read_text("utf-8"))
        # run.json is built from the ARCHIVED events copy, so its counts match the fixture.
        assert doc["rounds_total"] == 3 and doc["run_uid"] == "20260709T000017Z-4242"
        # the archived events copy is a faithful copy (same line count as the live log).
        assert (root / "events.jsonl").read_text("utf-8") == log.read_text("utf-8")

    def test_prune_keeps_newest_n(self, tmp_path):
        collab = str(tmp_path / "c")
        root = Path(collab) / "autopilot" / "history"
        root.mkdir(parents=True)
        # time-sortable names: run-01 .. run-10
        for i in range(1, 11):
            (root / f"run-{i:02d}").mkdir()
        rh.prune(collab, keep=3)
        remaining = sorted(p.name for p in root.iterdir())
        assert remaining == ["run-08", "run-09", "run-10"]

    def test_prune_missing_history_is_noop(self, tmp_path):
        rh.prune(str(tmp_path / "c"), keep=5)  # must not raise on an absent history dir


# --------------------------------------------------------------------------- #
# dashboard_core.set_max_rounds — range/type validation
# --------------------------------------------------------------------------- #


class TestSetMaxRounds:
    def test_valid_writes_control(self, tmp_path):
        collab = str(tmp_path / "c")
        dc.set_max_rounds(collab, 5, by="test")
        doc = json.loads(ap._control_path(collab).read_text("utf-8"))
        assert doc["max_rounds"] == 5 and doc["requested_by"] == "test"
        # and the driver's live reader picks it up as the live cap.
        assert ap._read_control(collab)["max_rounds"] == 5

    @pytest.mark.parametrize("bad", [0, 51, "x", True, -1])
    def test_invalid_raises_and_does_not_write(self, tmp_path, bad):
        collab = str(tmp_path / "c")
        with pytest.raises(ValueError):
            dc.set_max_rounds(collab, bad)
        assert not ap._control_path(collab).exists()  # rejected BEFORE any write


# --------------------------------------------------------------------------- #
# dashboard_core run_detail / compare_runs — path-safety
# --------------------------------------------------------------------------- #


def _write_run(collab, run_uid, doc, *, events=""):
    d = Path(collab) / "autopilot" / "history" / run_uid
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(doc), "utf-8")
    if events:
        (d / "events.jsonl").write_text(events, "utf-8")
    return d


class TestPathSafety:
    @pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".", "", "x" * 65])
    def test_run_detail_rejects_unsafe_ids(self, tmp_path, bad):
        with pytest.raises(ValueError):
            dc.run_detail(str(tmp_path / "c"), bad)

    @pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".", "", "x" * 65])
    def test_compare_runs_rejects_unsafe_ids(self, tmp_path, bad):
        collab = str(tmp_path / "c")
        _write_run(collab, "good-run", {"run_uid": "good-run", "rounds_total": 1})
        with pytest.raises(ValueError):
            dc.compare_runs(collab, bad, "good-run")
        with pytest.raises(ValueError):
            dc.compare_runs(collab, "good-run", bad)

    def test_run_detail_valid_id_returns_sections(self, tmp_path):
        collab = str(tmp_path / "c")
        events = (
            json.dumps({"stage": "autopilot.round", "role": "builder", "metrics": {"latency_ms": 5.0}})
            + "\n"
            + "{ torn line\n"
        )  # torn line skipped by the detail reader too
        _write_run(
            collab,
            "20260709T000000Z-1",
            {"run_uid": "20260709T000000Z-1", "rounds_total": 1, "lanes": {"confirmed": 2, "refuted": 0}},
            events=events,
        )
        detail = dc.run_detail(collab, "20260709T000000Z-1")
        assert detail["summary"]["rounds_total"] == 1
        assert detail["lanes"] == {"confirmed": 2, "refuted": 0}
        assert len(detail["events"]) == 1  # the torn line was dropped

    def test_compare_runs_valid_ids_return_deltas(self, tmp_path):
        collab = str(tmp_path / "c")
        _write_run(
            collab,
            "run-a",
            {
                "run_uid": "run-a",
                "rounds_total": 2,
                "calls": 2,
                "seat_calls": {"builder": 1},
                "phase_final": "capped",
                "signoff": {"result": "blocked"},
                "lanes": {"confirmed": 0, "refuted": 1},
            },
        )
        _write_run(
            collab,
            "run-b",
            {
                "run_uid": "run-b",
                "rounds_total": 5,
                "calls": 5,
                "seat_calls": {"builder": 3},
                "phase_final": "done",
                "signoff": {"result": "signed"},
                "lanes": {"confirmed": 2, "refuted": 1},
            },
        )
        cmp = dc.compare_runs(collab, "run-a", "run-b")
        assert cmp["deltas"]["rounds_total"] == 3  # b - a
        assert cmp["deltas"]["calls"] == 3
        assert cmp["deltas"]["seat_calls"]["builder"] == 2
        assert cmp["deltas"]["lanes"] == {"confirmed": 2, "refuted": 0}
        assert cmp["deltas"]["phase_final"] == {"a": "capped", "b": "done", "changed": True}
        assert cmp["deltas"]["signoff_result"] == {"a": "blocked", "b": "signed", "changed": True}


# --------------------------------------------------------------------------- #
# dashboard_core.list_runs — ordering + live current entry
# --------------------------------------------------------------------------- #


class TestListRuns:
    def test_newest_first_ordering(self, tmp_path):
        collab = str(tmp_path / "c")
        _write_run(collab, "20260709T000000Z-1", {"run_uid": "20260709T000000Z-1", "rounds_total": 1})
        _write_run(collab, "20260709T010000Z-2", {"run_uid": "20260709T010000Z-2", "rounds_total": 2})
        _write_run(collab, "20260708T230000Z-9", {"run_uid": "20260708T230000Z-9", "rounds_total": 9})
        runs = dc.list_runs(collab)
        assert [r["run_uid"] for r in runs] == [
            "20260709T010000Z-2",
            "20260709T000000Z-1",
            "20260708T230000Z-9",
        ]
        assert all(not r.get("current") for r in runs)  # no live run active

    def test_live_current_entry_synthesized_from_status(self, tmp_path):
        collab = str(tmp_path / "c")
        _write_run(collab, "20260709T000000Z-1", {"run_uid": "20260709T000000Z-1", "rounds_total": 1})
        # an active (non-terminal) status -> a synthesized current entry in front.
        ap._write_status(
            collab, run_uid="live-99", phase="thinking", started_ts="2026-07-09T02:00:00Z", round=2
        )
        runs = dc.list_runs(collab)
        assert runs[0]["current"] is True and runs[0]["run_uid"] == "live-99"
        assert runs[0]["rounds_total"] == 2
        assert [r["run_uid"] for r in runs[1:]] == ["20260709T000000Z-1"]

    def test_terminal_status_is_not_current(self, tmp_path):
        collab = str(tmp_path / "c")
        _write_run(collab, "20260709T000000Z-1", {"run_uid": "20260709T000000Z-1", "rounds_total": 1})
        ap._write_status(collab, run_uid="done-run", phase="done")
        runs = dc.list_runs(collab)
        assert all(not r.get("current") for r in runs)  # a done run is history, not "current"


# --------------------------------------------------------------------------- #
# INTEGRATION / E2E — driver run-start wipe + run-end archive (fake runner)
# --------------------------------------------------------------------------- #


class TestDriverArchive:
    def test_run_archives_history_and_wipes_live_log(self, tmp_path):
        # Drive a real run() with a fake runner. Pre-seed the live log with junk to prove the run-start wipe;
        # assert the run is archived on exit (history/<run_uid>/ with run.json + events.jsonl) and that the
        # live log reflects ONLY this run (the junk is gone).
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="kickoff", body="review this")
        live = Path(ap._log_default(collab))
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(json.dumps({"stage": "JUNK_FROM_PRIOR_RUN", "marker": "STALE"}) + "\n", "utf-8")

        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            if "builder" in cmd[0]:
                n["b"] += 1
                return f"rev {n['b']}"  # distinct output per attempt -> genuine progress
            return "keep going"  # reviewer withholds -> repair to the work-attempt budget

        rounds = ap.run(
            collab, seats=_cli(["reviewer", "builder"]), max_rounds=2, watch=False, runner=runner, home=home
        )
        assert rounds >= 2  # no sign-off -> loops to the work-attempt budget, then escalates

        # run_uid was minted + stamped into status.json.
        run_uid = dc.read_status(collab)["run_uid"]
        hist = Path(collab) / "autopilot" / "history" / run_uid
        assert hist.is_dir(), f"expected archive at {hist}"
        assert (hist / "run.json").exists() and (hist / "events.jsonl").exists()
        run_doc = json.loads((hist / "run.json").read_text("utf-8"))
        for key in ("run_uid", "rounds_total", "calls", "seat_calls", "lanes", "signoff", "handoffs_touched"):
            assert key in run_doc
        assert run_doc["run_uid"] == run_uid

        # the LIVE log was wiped at start: the stale junk is gone, and it holds this run's own events.
        live_text = live.read_text("utf-8")
        assert "STALE" not in live_text and "JUNK_FROM_PRIOR_RUN" not in live_text
        assert "autopilot.round" in live_text
        # the archived copy likewise carries this run and not the junk.
        assert "STALE" not in (hist / "events.jsonl").read_text("utf-8")

    def test_run_json_records_truthful_terminal(self, tmp_path):
        # ADR-0003: a run that escalates must record the truthful terminal in run.json — signoff.result
        # "escalated", the terminal_reason, an escalation count, and the per-outcome tally.
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="kickoff", body="build it")
        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            if "builder" in cmd[0]:
                n["b"] += 1
                return f"rev {n['b']}"  # distinct output -> genuine progress, loops to the budget
            return "not yet — keep going"  # reviewer withholds -> repair_required each attempt

        ap.run(collab, seats=_cli(["reviewer", "builder"]), max_rounds=2, runner=runner, home=home)
        run_uid = dc.read_status(collab)["run_uid"]
        run_doc = json.loads(
            (Path(collab) / "autopilot" / "history" / run_uid / "run.json").read_text("utf-8")
        )
        assert run_doc["signoff"]["result"] == "escalated"
        assert run_doc["terminal_reason"] == "budget_exhausted"
        assert run_doc["escalations"] >= 1
        assert run_doc["outcomes"].get("repair_required", 0) >= 1

    def test_archive_happens_even_on_stall_exit(self, tmp_path):
        # A backend stall is a non-graceful exit path; the finally-archival must still capture the run.
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")

        def crash(*a, **k):
            raise cc.CollabError("backend died")

        ap.run(collab, seats=_cli(["reviewer"]), max_rounds=5, runner=crash, home=home)
        run_uid = dc.read_status(collab)["run_uid"]
        hist = Path(collab) / "autopilot" / "history" / run_uid
        assert (hist / "run.json").exists()  # even a stalled run is preserved


# --------------------------------------------------------------------------- #
# INTEGRATION — the live max-rounds knob is honored by the per-thread loop
# --------------------------------------------------------------------------- #


class TestLiveMaxRounds:
    def test_read_control_returns_live_max_rounds(self, tmp_path):
        collab = str(tmp_path / "c")
        # positive int -> surfaced; invalid (0, bool, string, negative) -> None (keep launch fallback).
        dc.set_max_rounds(collab, 4)
        assert ap._read_control(collab)["max_rounds"] == 4
        # write raw invalid values directly (set_max_rounds would reject them) and confirm they read as None.
        p = ap._control_path(collab)
        for bad in (0, -3, True, "nope"):
            p.write_text(json.dumps({"max_rounds": bad}), "utf-8")
            assert ap._read_control(collab)["max_rounds"] is None

    @staticmethod
    def _work_attempts(collab):
        rec = json.loads((Path(collab) / "autopilot" / "budget" / "001.json").read_text("utf-8"))
        return rec["current"]["work_attempts"]

    def test_loop_honors_a_raised_live_cap_beyond_launch_max(self, tmp_path):
        # The strongest deterministic proof: launch with max_rounds=1 (work_attempts budget = 1), but a runner
        # that RAISES the live cap to 3 on each builder turn. The loop re-reads control.json every iteration
        # and reconstructs the RunBudget with the new ceiling, so it charges 3 work attempts — beyond the
        # launch-time cap of 1 — proving the live max_rounds (not the launch value) governs the budget.
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="kickoff", body="review this")
        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            if "builder" in cmd[0]:
                n["b"] += 1
                dc.set_max_rounds(collab, 3)  # bump the live ceiling mid-run
                return f"rev {n['b']}"  # distinct output per attempt -> genuine progress
            return "keep going"  # reviewer withholds

        ap.run(collab, seats=_cli(["reviewer", "builder"]), max_rounds=1, runner=runner, home=home)
        assert self._work_attempts(collab) == 3  # loop honored the raised live cap beyond the launch max of 1

    def test_loop_honors_a_lowered_live_cap(self, tmp_path):
        # The inverse: a pre-set live cap of 1 must LOWER an otherwise-generous launch budget of 5.
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="kickoff", body="review this")
        dc.set_max_rounds(collab, 1)  # live cap lower than the launch max_rounds
        n = {"b": 0}

        def runner(cmd, prompt, *, timeout, **kw):
            if "builder" in cmd[0]:
                n["b"] += 1
                return f"rev {n['b']}"
            return "keep going"

        ap.run(collab, seats=_cli(["reviewer", "builder"]), max_rounds=5, runner=runner, home=home)
        assert self._work_attempts(collab) == 1  # the live cap (1) won over the launch cap (5)


# --------------------------------------------------------------------------- #
# ENDPOINT SMOKE — GET /api/runs, /api/run, /api/compare; POST /api/max-turns
# --------------------------------------------------------------------------- #


class TestHistoryEndpoints:
    def _serve(self, collab, home):
        import dashboard_web as dw

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), dw._Handler)
        httpd.collab = str(collab)
        httpd.home = home
        httpd.token = "test-token"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def _req(self, url, *, data=None, headers=None):
        # Retry once on a Windows connection-abort (an RST race when the server replies 403/400 without
        # draining the request body). Every request here is a read or an idempotent write, so a retry is safe.
        last = None
        for _ in range(2):
            req = urllib.request.Request(
                url, data=data, headers=headers or {}, method="POST" if data is not None else "GET"
            )
            try:
                r = urllib.request.urlopen(req, timeout=5)
                return r.getcode(), r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
            except (ConnectionError, urllib.error.URLError, OSError) as e:
                last = e
        raise AssertionError(f"request to {url} failed twice: {last!r}")

    def test_runs_run_compare_and_max_turns(self, tmp_path):
        collab = str(tmp_path / "c")
        _write_run(
            collab,
            "20260709T000000Z-1",
            {
                "run_uid": "20260709T000000Z-1",
                "rounds_total": 2,
                "calls": 2,
                "phase_final": "capped",
                "lanes": {"confirmed": 0, "refuted": 1},
                "signoff": {"result": "blocked"},
                "seat_calls": {"builder": 1},
            },
            events=json.dumps({"stage": "autopilot.round", "role": "builder", "metrics": {"latency_ms": 5.0}})
            + "\n",
        )
        _write_run(
            collab,
            "20260709T010000Z-2",
            {
                "run_uid": "20260709T010000Z-2",
                "rounds_total": 5,
                "calls": 5,
                "phase_final": "done",
                "lanes": {"confirmed": 2, "refuted": 0},
                "signoff": {"result": "signed"},
                "seat_calls": {"builder": 3},
            },
        )
        httpd, port = self._serve(collab, str(tmp_path))
        try:
            base = f"http://127.0.0.1:{port}"
            tok = {"Content-Type": "application/json", "X-Dash-Token": "test-token"}

            # GET /api/runs — newest first.
            code, body = self._req(base + "/api/runs")
            runs = json.loads(body)
            assert code == 200
            assert [r["run_uid"] for r in runs] == ["20260709T010000Z-2", "20260709T000000Z-1"]

            # GET /api/run?id= — detail for one run.
            code, body = self._req(base + "/api/run?id=20260709T000000Z-1")
            detail = json.loads(body)
            assert code == 200
            assert detail["summary"]["rounds_total"] == 2
            assert len(detail["events"]) == 1

            # GET /api/run?id=../x — bad id rejected as 400.
            assert self._req(base + "/api/run?id=../x")[0] == 400

            # GET /api/compare?a=&b= — delta JSON.
            code, body = self._req(base + "/api/compare?a=20260709T000000Z-1&b=20260709T010000Z-2")
            cmp = json.loads(body)
            assert code == 200 and cmp["deltas"]["rounds_total"] == 3

            # POST /api/max-turns with the token -> control.json updated.
            code, _ = self._req(base + "/api/max-turns", data=b'{"n":7}', headers=tok)
            assert code == 200
            assert json.loads(ap._control_path(collab).read_text("utf-8"))["max_rounds"] == 7

            # POST /api/max-turns without the token -> 403 (rejected before any write).
            assert (
                self._req(
                    base + "/api/max-turns", data=b'{"n":9}', headers={"Content-Type": "application/json"}
                )[0]
                == 403
            )
            # out-of-range n -> 400.
            assert self._req(base + "/api/max-turns", data=b'{"n":99}', headers=tok)[0] == 400
            # the durable value is still 7 (the bad requests never wrote).
            assert json.loads(ap._control_path(collab).read_text("utf-8"))["max_rounds"] == 7
        finally:
            httpd.shutdown()
            httpd.server_close()
