"""Tests for the autopilot dashboard: the emission instrumentation added to autopilot.py plus the
shared read/act layer in dashboard_core.py.

Like test_autopilot.py, the agent backend is injected (``runner=``) so the whole loop runs with a FAKE
agent — no real CLI, no network. These tests pin the observability contract (events + status land, and
are best-effort) and the human-gated control contract (pause idles the loop; approve — and only a human
— advances a handoff), so the [C15]/[C36] guarantees don't silently regress.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import collab_common as cc  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402


def _cli(seat_names):
    return {s: {"backend": "cli", "cmd": [f"model-{s}"], "system": f"You are the {s}."} for s in seat_names}


def _events(collab):
    return dc.tail_events(collab, 500)


def _stages(collab):
    return [e.get("stage") for e in _events(collab)]


# --------------------------------------------------------------------------- #
# emission
# --------------------------------------------------------------------------- #


class TestEmission:
    def test_run_emits_start_claim_and_turn(self, tmp_path):
        # ADR-0001: a turn is not a handoff, so there is no per-turn handoff.create edge anymore. Drive one
        # exchange step through run() and assert the round span + the reused claim edge land in telemetry.
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="please review", body="review this")
        ap.run(collab, seats=_cli(["reviewer"]), runner=lambda *a, **k: "approved with nits",
               max_rounds=3, home=home)

        evs = _events(collab)
        stages = [e.get("stage") for e in evs]
        # autopilot is visible to telemetry: the round span (start + the turn) + the reused claim edge
        assert stages.count("autopilot.round") == 2          # start + turn
        assert "review" in stages                             # claim edge (he.on_claim), emitted once by run()

        turn = [e for e in evs if e.get("stage") == "autopilot.round"
                and (e.get("decision") or {}).get("action") == "turn"][0]
        m = turn.get("metrics") or {}
        assert isinstance(m.get("latency_ms"), (int, float)) and m["latency_ms"] >= 0
        assert m.get("resp_bytes") == len("approved with nits".encode("utf-8"))
        assert turn.get("artifact") == "handoff:001"

    def test_backend_failure_emits_fail_event_and_leaves_claimed(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")

        def crash(*a, **k):
            raise cc.CollabError("boom")

        ap.run(collab, seats=_cli(["reviewer"]), runner=crash, max_rounds=5, home=str(tmp_path))
        assert hc.state_of(collab, "001") == "claimed"        # handoff stays claimed for a human ([C39])
        fails = [e for e in _events(collab) if (e.get("decision") or {}).get("action") == "fail"]
        assert len(fails) == 1
        assert (fails[0].get("failure") or {}).get("kind") == "backend"
        assert "boom" in (fails[0].get("failure") or {}).get("message", "")

    def test_telemetry_failure_never_breaks_a_round(self, tmp_path, monkeypatch):
        # [C15]: a committed state change must survive a telemetry outage. Break the emitter entirely.
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        monkeypatch.setattr(ap._trace, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no log")))
        ap.run(collab, seats=_cli(["reviewer"]), runner=lambda *a, **k: "ok", max_rounds=3, home=str(tmp_path))
        assert hc.state_of(collab, "001") == "claimed"        # the claim really committed despite the outage
        arts = sorted((Path(collab) / "autopilot" / "replies").glob("*.md"))
        assert arts and "ok" in arts[0].read_text("utf-8")    # the turn was persisted, not lost


# --------------------------------------------------------------------------- #
# status.json + control.json
# --------------------------------------------------------------------------- #


class TestStatusAndControl:
    def test_status_written_and_readable(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        hc.claim(collab, "001")
        ap._dispatch_seat(collab, "reviewer", seats=_cli(["reviewer"]), runner=lambda *a, **k: "done",
                          hid="001", transcript="y", log=ap._log_default(collab),
                          rid=ap._run_id(collab), attempt=2, span_role="builder")
        st = dc.read_status(collab)
        assert st is not None
        assert st["round"] == 2
        assert st["current_hid"] is None                      # cleared at round end
        assert st["last_latency_ms"] is not None
        assert json.loads(ap._status_path(collab).read_text("utf-8"))["schema_version"] == "0.1"

    def test_control_round_trip(self, tmp_path):
        collab = str(tmp_path / "c")
        assert dc.read_control(collab)["paused"] is False     # safe default when absent
        dc.set_paused(collab, True, by="test")
        ctrl = dc.read_control(collab)
        assert ctrl["paused"] is True and ctrl["requested_by"] == "test"
        dc.set_paused(collab, False)
        assert dc.read_control(collab)["paused"] is False

    def test_pause_gate_idles_the_loop_reversibly(self, tmp_path, monkeypatch):
        # [C36]: a pause file must make run() IDLE (claim nothing) rather than progress. A paused loop
        # waits for a human, so we prove "it reached the idle sleep with zero work done" by making the
        # first sleep raise (same technique as test_watch_polls_on_idle) — never actually waiting.
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="review")
        seats = _cli(["reviewer", "builder"])
        dc.set_paused(collab, True)

        class _Slept(Exception):
            pass

        monkeypatch.setattr(ap.time, "sleep", lambda _s: (_ for _ in ()).throw(_Slept()))
        with pytest.raises(_Slept):
            ap.run(collab, seats=seats, max_rounds=3, runner=lambda *a, **k: "ok", home=str(tmp_path))
        assert dc.read_status(collab)["phase"] == "paused"
        assert hc.state_of(collab, "001") == "pending"        # nothing was claimed while paused
        monkeypatch.undo()

        dc.set_paused(collab, False)                          # resume -> it now runs to the cap
        rounds = ap.run(collab, seats=seats, max_rounds=3, runner=lambda *a, **k: "ok", home=str(tmp_path))
        assert rounds == 3


# --------------------------------------------------------------------------- #
# snapshot aggregation + human actions
# --------------------------------------------------------------------------- #


class TestSnapshotAndActions:
    def test_snapshot_aggregates_board_events_and_seats(self, tmp_path):
        home = str(tmp_path)
        collab = str(tmp_path / "c")
        # Two seats: an explicit-cmd seat with NO model field / NO --model flag (launcher only, model None),
        # and a catalog-composed seat whose chosen model id must surface. A `models` catalog composes the
        # latter's runnable cmd (via ap.load_seats), but snapshot must redact it to launcher + id only.
        (tmp_path / "seats.json").write_text(json.dumps({
            "models": {"opus": {"cmd": ["claude", "-p", "-", "--model", "opus"]}},
            "seats": {
                "reviewer": {"backend": "cli", "cmd": ["gpt-5.5", "--flag", "secret"], "system": "r"},
                "builder": {"backend": "cli", "model": "opus", "system": "b"}}}), "utf-8")
        hc.create(collab, to="reviewer", from_="builder", title="a", body="one")
        hc.create(collab, to="builder", from_="reviewer", title="b", body="two")

        snap = dc.snapshot(collab, home=home)
        assert snap["counts"]["pending"] == 2
        assert [r["id"] for r in snap["open"]] == ["001", "002"]
        assert snap["seats"]["reviewer"]["launcher"] == "gpt-5.5"       # cmd[0] only...
        assert snap["seats"]["reviewer"]["model"] is None              # ...no model field, no --model flag
        assert snap["seats"]["builder"]["model"] == "opus"             # the chosen catalog id surfaces
        assert "secret" not in json.dumps(snap["seats"])                # never the full argv ([C38])
        assert snap["paused"] is False

    def test_snapshot_events_tail_is_ordered_and_limited(self, tmp_path):
        collab = str(tmp_path / "c")
        log = ap._log_default(collab)
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(json.dumps({"ts": f"t{i}", "stage": "x", "n": i}) + "\n")
            f.write("{ this is not json\n")                              # a torn final line
        evs = dc.tail_events(collab, limit=4)
        assert [e["n"] for e in evs] == [6, 7, 8, 9]                     # newest kept, oldest-first, torn line skipped

    def test_advance_handoff_advances_and_logs(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        r = dc.advance_handoff(collab, "001")
        assert r == {"id": "001", "state": "done", "changed": True}
        assert hc.state_of(collab, "001") == "done"
        assert "handoff.done" in _stages(collab)                        # the human sign-off is audited
        # idempotent: approving a done handoff is a no-op, not a crash
        assert dc.advance_handoff(collab, "001")["changed"] is False

    def test_advance_missing_handoff_raises(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")  # ensure layout exists
        with pytest.raises(hc.HandoffNotFound):
            dc.advance_handoff(collab, "999")

    def test_no_reverse_transition_exists(self, tmp_path):
        # Documents the design: the core is forward-only (claim/done/archive). A dashboard "re-queue"
        # therefore cannot move claimed->pending in place; nudge() creates a NEW pending handoff instead.
        assert set(hc._TRANSITIONS) == {"claim", "done", "archive"}
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="stuck", body="z")
        hc.claim(collab, "001")
        new = dc.nudge(collab, "001")
        assert new["id"] == "002" and hc.state_of(collab, "002") == "pending"
        assert hc.state_of(collab, "001") == "claimed"                  # original untouched


class TestSeatModelControl:
    """The dashboard "any model in any seat" control: set_seat_model rewrites a seat's catalog id and
    ap.load_seats composes the new runnable cmd from the top-level catalog."""

    def _home(self, tmp_path):
        (tmp_path / "seats.json").write_text(json.dumps({
            "models": {
                "opus": {"cmd": ["claude", "-p", "-", "--model", "opus"], "unset_env": ["ANTHROPIC_API_KEY"]},
                "gpt-5.5": {"cmd": ["adapter", "--model", "gpt-5.5"]},
                "_note": {"cmd": ["ignored"]},  # underscore-prefixed: not a real selectable id
            },
            "seats": {
                "builder": {"backend": "cli", "model": "opus", "model_args": ["--perm", "all"], "system": "b"},
                "reviewer": {"backend": "cli", "model": "gpt-5.5", "system": "r", "can_sign_off": True},
                "human": {"backend": "bridge"},
            }}, indent=2), "utf-8")
        return str(tmp_path)

    def test_change_is_visible_through_load_seats(self, tmp_path):
        home = self._home(tmp_path)
        r = dc.set_seat_model(home, "builder", "gpt-5.5")
        assert r == {"seat": "builder", "model": "gpt-5.5", "by": "dashboard"}
        seats = ap.load_seats(home)
        # composed cmd = catalog template + the seat's own model_args, and preserved fields survive
        assert seats["builder"]["cmd"] == ["adapter", "--model", "gpt-5.5", "--perm", "all"]
        assert seats["builder"]["system"] == "b" and seats["builder"]["model_args"] == ["--perm", "all"]
        assert seats["reviewer"]["can_sign_off"] is True                # other seat untouched

    def test_switch_back_to_a_subscription_model_inherits_unset_env(self, tmp_path):
        home = self._home(tmp_path)
        dc.set_seat_model(home, "reviewer", "opus")
        seats = ap.load_seats(home)
        assert seats["reviewer"]["cmd"] == ["claude", "-p", "-", "--model", "opus"]
        assert seats["reviewer"]["unset_env"] == ["ANTHROPIC_API_KEY"]   # inherited from the catalog entry

    def test_unknown_model_raises(self, tmp_path):
        home = self._home(tmp_path)
        with pytest.raises(cc.CollabError) as e:
            dc.set_seat_model(home, "builder", "no-such-model")
        assert "gpt-5.5" in str(e.value) and "opus" in str(e.value)      # message lists the valid ids
        assert ap.load_seats(home)["builder"]["cmd"][0] == "claude"      # unchanged (opus still)

    def test_unknown_seat_raises(self, tmp_path):
        home = self._home(tmp_path)
        with pytest.raises(cc.CollabError):
            dc.set_seat_model(home, "ghost", "opus")

    def test_non_cli_seat_raises(self, tmp_path):
        home = self._home(tmp_path)
        with pytest.raises(cc.CollabError):
            dc.set_seat_model(home, "human", "opus")

    def test_snapshot_includes_models_catalog(self, tmp_path):
        home = self._home(tmp_path)
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        snap = dc.snapshot(collab, home=home)
        assert snap["models_catalog"] == ["gpt-5.5", "opus"]            # sorted, underscore ids excluded

    def test_v2_switch_fails_before_write_when_it_breaks_profile_diversity(self, tmp_path):
        example = Path(__file__).resolve().parent.parent / "seats.example.json"
        target = tmp_path / "seats.json"
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

        with pytest.raises(cc.CollabError, match="disjoint"):
            # The baseline profile inherits breaker.model.  Making it OpenAI
            # would overlap the OpenAI high-risk breaker, so the dashboard must
            # leave the durable config untouched.
            dc.set_seat_model(str(tmp_path), "breaker", "gpt-5.6-luna")

        persisted = json.loads(target.read_text(encoding="utf-8"))
        assert persisted["seats"]["breaker"]["model"] == "opus-4.8"


class TestRiskTieredLaneEvidence:
    def test_latest_lanes_reads_nested_candidate_ledger_and_profile_badges(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        lanes.write_ledger(collab, "001", {
            "hid": "001", "tests": {"passed": True}, "blockers": [],
            "verification_plan_digest": "plan:abc",
            "lanes": [{
                "pass": "high-risk-diverse", "contracts": ["order-risk-and-idempotency"],
                "profile": {"id": "high-risk-diverse",
                            "breaker": {"seat": "breaker"}, "verifier": {"seat": "verifier"}},
                "ran": True, "confirmed": [], "refuted": [], "composite": True,
            }],
        }, candidate_id="cand:v2")

        latest = dc._latest_lanes(collab)
        assert latest is not None and latest["plan_digest"] == "plan:abc"
        assert latest["lanes"] == [{
            "lane": "high-risk-diverse", "pass": "high-risk-diverse",
            "profile": "high-risk-diverse", "contracts": ["order-risk-and-idempotency"],
            "composite": True, "ran": True, "incomplete": False,
            "confirmed": 0, "refuted": 0, "breaker": "breaker", "verifier": "verifier",
        }]


# --------------------------------------------------------------------------- #
# stats aggregation (the informational upgrade)
# --------------------------------------------------------------------------- #


def _round_ev(seat, action, *, ms=None, rb=None, hid="001", ts="2026-07-05T00:00:00Z"):
    ev = {"schema_version": "0.1", "ts": ts, "stage": "autopilot.round", "role": seat,
          "artifact": f"handoff:{hid}", "decision": {"action": action, "reason_codes": []}}
    m = {}
    if ms is not None:
        m["latency_ms"] = ms
    if rb is not None:
        m["resp_bytes"] = rb
    if m:
        ev["metrics"] = m
    return ev


class TestStats:
    def test_run_stats_aggregates_per_seat_and_overall(self):
        evs = [
            _round_ev("reviewer", "start"),
            _round_ev("reviewer", "turn", ms=100, rb=10, hid="001"),
            _round_ev("builder", "turn", ms=200, rb=20, hid="002"),
            _round_ev("reviewer", "turn", ms=300, rb=30, hid="003"),
            _round_ev("reviewer", "fail", hid="004"),
        ]
        st = dc.run_stats(evs, series_n=2)
        assert st["overall"]["rounds"] == 3 and st["overall"]["fails"] == 1
        assert st["overall"]["avg_ms"] == 200.0                          # (100+200+300)/3, fails excluded
        rv = st["seats"]["reviewer"]
        assert rv["rounds"] == 2 and rv["fails"] == 1
        assert rv["avg_ms"] == 200.0 and rv["last_ms"] == 300.0          # last successful turn
        assert rv["total_resp_bytes"] == 40
        assert st["seats"]["builder"]["rounds"] == 1
        assert len(st["latency_series"]) == 2                            # capped at series_n
        tail = st["latency_series"][-1]
        assert tail["ms"] == 300.0 and tail["seat"] == "reviewer" and tail["hid"] == "003"

    def test_run_stats_defensive(self):
        evs = ["notadict", {"stage": "other"},
               _round_ev("r", "turn", ms="nope"),   # non-numeric latency -> counted, no avg
               _round_ev("r", "turn", ms=True)]      # bool is NOT a number -> counted, no avg
        st = dc.run_stats(evs)
        assert st["overall"]["rounds"] == 2 and st["overall"]["avg_ms"] is None
        assert st["latency_series"] == []
        empty = dc.run_stats([])
        assert empty["overall"] == {"rounds": 0, "fails": 0, "avg_ms": None} and empty["seats"] == {}

    def test_snapshot_includes_stats_from_single_read(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        # run_stats aggregates 'turn'-action round events (see TestStats). Emit one completed-round event
        # onto the same event stream the driver appends to, so the snapshot has round telemetry to fold in;
        # the point of this test is that stats + the event feed come from ONE read, not from two.
        log = ap._log_default(collab)
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(_round_ev("reviewer", "turn", ms=12, rb=2)) + "\n")
        snap = dc.snapshot(collab)
        assert snap["stats"]["overall"]["rounds"] >= 1
        assert len(snap["events"]) <= 60
        assert dc.run_stats(dc.read_events(collab)) == snap["stats"]     # feed + stats from ONE read


class TestHandoffView:
    def test_reply_pointer_resolves_to_artifact_text(self, tmp_path):
        # dashboard handoff_view must resolve an AUTOPILOT_REPLY pointer body to the real (untrusted) artifact
        # text, not surface the raw pointer. ADR-0001 removed the auto-created reply handoff, but the pointer
        # shape + its resolution are still a live feature, so we build a pointer-bodied handoff directly.
        collab = str(tmp_path / "c")
        rel = ap._write_reply(collab, "reviewer", "my full review verdict here")
        hc.create(collab, to="builder", from_="reviewer", title="reply", body=f"AUTOPILOT_REPLY {rel}")
        v = dc.handoff_view(collab, "001")
        assert v["is_reply"] is True
        assert v["frontmatter"]["to"] == "builder" and v["frontmatter"]["from"] == "reviewer"
        assert "my full review verdict here" in v["body_text"]           # the real artifact text, not the pointer
        hc.create(collab, to="reviewer", from_="builder", title="x", body="please review this")
        p = dc.handoff_view(collab, "002")
        assert p["is_reply"] is False and "please review this" in p["body_text"]

    def test_unknown_raises(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        with pytest.raises(hc.HandoffNotFound):
            dc.handoff_view(collab, "999")

    def test_frontmatter_is_whitelisted(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        v = dc.handoff_view(collab, "001")
        assert set(v["frontmatter"]) <= {"to", "from", "title", "priority", "date", "status"}
        assert "id" not in v["frontmatter"]                              # non-whitelisted keys dropped


class TestHidValidation:
    def test_hid_regex_accepts_ids_rejects_junk(self):
        import dashboard_web as dw
        for good in ("001", "42", "1", "999999999"):
            assert dw._HID_RE.fullmatch(good)
        for bad in ("", "1a", "../x", "1" * 10, " 1", "1 ", "0x1", "1/2"):
            assert not dw._HID_RE.fullmatch(bad)


class TestHttpLayer:
    """Locks the route wiring the core-function tests don't cover: hid validation, token gate, error codes."""

    def _serve(self, collab, home):
        import threading
        import dashboard_web as dw
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), dw._Handler)
        httpd.collab = str(collab)
        httpd.home = home
        httpd.token = "test-token"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    def _code(self, url, *, data=None, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.getcode(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_endpoints_and_security(self, tmp_path):
        import json as _json
        collab = str(tmp_path / "c")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="hello body text")
        httpd, port = self._serve(collab, str(tmp_path))
        try:
            base = f"http://127.0.0.1:{port}"
            code, body = self._code(base + "/api/handoff?hid=001")
            assert code == 200 and "hello body text" in _json.loads(body)["body_text"]
            assert self._code(base + "/api/handoff?hid=../x")[0] == 400
            assert self._code(base + "/api/handoff?hid=999")[0] == 404
            # POST without the token is rejected
            assert self._code(base + "/api/nudge", data=b'{"hid":"001"}',
                              headers={"Content-Type": "application/json"})[0] == 403
            # POST with the token works
            code, _ = self._code(base + "/api/nudge", data=b'{"hid":"001"}',
                                 headers={"Content-Type": "application/json", "X-Dash-Token": "test-token"})
            assert code == 200 and hc.state_of(collab, "002") == "pending"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_seat_model_endpoint(self, tmp_path):
        import json as _json
        collab = str(tmp_path / "c")
        (tmp_path / "seats.json").write_text(_json.dumps({
            "models": {"opus": {"cmd": ["claude", "-p", "-", "--model", "opus"]},
                       "gpt-5.5": {"cmd": ["adapter", "--model", "gpt-5.5"]}},
            "seats": {"reviewer": {"backend": "cli", "model": "opus", "system": "r"}}}), "utf-8")
        hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
        httpd, port = self._serve(collab, str(tmp_path))
        try:
            base = f"http://127.0.0.1:{port}"
            tok = {"Content-Type": "application/json", "X-Dash-Token": "test-token"}
            # no token -> rejected before any config write
            assert self._code(base + "/api/seat-model", data=b'{"seat":"reviewer","model":"gpt-5.5"}',
                              headers={"Content-Type": "application/json"})[0] == 403
            # a bad seat name (regex) -> 400
            assert self._code(base + "/api/seat-model", data=b'{"seat":"../x","model":"opus"}', headers=tok)[0] == 400
            # a validly-shaped but unknown model -> CollabError mapped to 400 (bad input, not 500)
            assert self._code(base + "/api/seat-model", data=b'{"seat":"reviewer","model":"nope"}',
                              headers=tok)[0] == 400
            # valid -> 200 and the change is durable
            code, body = self._code(base + "/api/seat-model", data=b'{"seat":"reviewer","model":"gpt-5.5"}',
                                    headers=tok)
            assert code == 200 and _json.loads(body)["model"] == "gpt-5.5"
            assert ap.load_seats(str(tmp_path))["reviewer"]["cmd"] == ["adapter", "--model", "gpt-5.5"]
        finally:
            httpd.shutdown()
            httpd.server_close()


# --------------------------------------------------------------------------- #
# Phase 7: durable reopen (retry/adopt) requests + start_driver guard
# --------------------------------------------------------------------------- #


class TestReopenAndStart:
    def test_reopen_files_a_durable_retry_request(self, tmp_path):
        import operator_requests as opreq
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        hc.claim(collab, "001")  # a paused/claimed handoff
        res = dc.reopen_handoff(collab, "001", by="dashboard-web")
        assert res == {"id": "001", "state": "claimed", "action": "retry", "queued": True}
        assert opreq.get(collab, "001")["action"] == "retry"          # durable request written
        assert any(e.get("stage") == "autopilot.control"
                   and (e.get("decision") or {}).get("action") == "reopen" for e in _events(collab))

    def test_reopen_adopt_action(self, tmp_path):
        import operator_requests as opreq
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        dc.reopen_handoff(collab, "001", action="adopt")
        assert opreq.get(collab, "001")["action"] == "adopt"

    def test_reopen_unknown_handoff_raises(self, tmp_path):
        with pytest.raises(hc.HandoffNotFound):
            dc.reopen_handoff(str(tmp_path / "c"), "999")

    def test_reopen_closed_handoff_conflicts(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        hc.claim(collab, "001")
        hc.done(collab, "001")
        with pytest.raises(hc.HandoffConflict):
            dc.reopen_handoff(collab, "001")           # nothing to retry on a closed handoff

    def test_start_driver_spawns_and_reports_pid(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        seen = {}

        def fake_spawn(cmd):
            seen["cmd"] = cmd
            return 4242

        res = dc.start_driver(collab, str(tmp_path), max_rounds=5, spawn=fake_spawn)
        assert res["started"] is True and res["pid"] == 4242
        assert "--collab" in seen["cmd"] and "--watch" in seen["cmd"]
        assert "--max-rounds" in seen["cmd"] and "5" in seen["cmd"]

    def test_start_driver_refuses_when_a_live_driver_holds_the_board(self, tmp_path):
        collab = str(tmp_path / "c")
        hc.create(collab, to="builder", from_="reviewer", title="x", body="y")
        hc.ActiveHandoffLease(collab, "live-run").acquire("001")   # a live board lease

        def boom(cmd):
            raise AssertionError("must not spawn a second driver while one holds the board")

        with pytest.raises(cc.CollabError):
            dc.start_driver(collab, str(tmp_path), spawn=boom)
