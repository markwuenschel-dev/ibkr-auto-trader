"""Tests for narrative.py — the human-readable "what happened" story of a handoff.

build() stitches the handoff's own prose (Summary/Deliverables) with one line per builder/reviewer turn
(seeded from the real reply artifacts) and the landing facts (state/tests/sign-off). These tests pin: the
story shape, honest reflection of the outcome (signed-off / capped / blocked / done-by-hand), the two
frontmatter-list spellings, blocker detection, write() idempotency, read-only-ness of build(), and
graceful degradation when there are no events / no handoff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import contracts  # noqa: E402
import handoff_core as hc  # noqa: E402
import narrative  # noqa: E402

_HANDOFF = """---
to: builder
from: reviewer
id: 001-thing
title: Build the thing → and wire it
priority: normal
guardrails: [money, auth]
depends_on: [PT-1 domain models, PT-2 state store]
adr: docs/design/adr/0001.md
---

## Summary

Implement the thing that produces a frozen snapshot. This is why it matters: downstream depends on it.

A second paragraph with more detail that should be trimmed away past the cap.

## Deliverables

- `src/thing.py` — the Thing protocol and error hierarchy.
- `tests/test_thing.py` — the DoD test surface.
"""


def _handoff(collab, body=_HANDOFF):
    """Create handoff 001 and give it rich frontmatter + sections (hc.create writes only a stub)."""
    res = hc.create(collab, to="builder", from_="reviewer", title="stub", body="stub")
    Path(res["path"]).write_text(body, encoding="utf-8")
    return res["id"]


def _reply(collab, name, text):
    d = Path(collab) / "autopilot" / "replies"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return f"autopilot/replies/{name}"


def _ev(hid, round_no, role, relpath, *, lat=1000.0):
    return {"schema_version": "0.1", "ts": f"2026-01-01T00:0{round_no}:00Z", "run_id": "t",
            "span_id": f"r{round_no}:{role}:done", "parent_span_id": f"r{round_no}:{role}",
            "stage": "autopilot.round", "role": role, "artifact": f"handoff:{hid}",
            "decision": {"action": "turn", "reason_codes": [f"reply:{relpath}"], "confidence": None},
            "metrics": {"latency_ms": lat, "resp_bytes": 50}}


def _write_events(collab, events):
    p = Path(collab) / "logs" / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _two_turns(collab, hid, *, reviewer_text="Looks correct. Approving.", extra=None):
    r1 = _reply(collab, "r1-builder.md", "I implemented the Thing protocol and its tests. All green.")
    r2 = _reply(collab, "r2-reviewer.md", reviewer_text)
    events = [_ev(hid, 1, "builder", r1, lat=66700.0), _ev(hid, 2, "reviewer", r2, lat=16300.0)]
    if extra:
        events += extra
    _write_events(collab, events)


class TestBuild:
    def test_story_has_all_sections(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)
        md = narrative.build(collab, hid)
        assert md.startswith("<!-- autopilot-narrative:001 -->")
        assert md.rstrip().endswith("<!-- /autopilot-narrative:001 -->")
        assert "Build the thing → and wire it" in md              # title (unicode preserved)
        assert "why it matters" in md                             # Summary prose surfaced
        assert "Guardrails: money, auth" in md
        assert "Depends on: PT-1 domain models, PT-2 state store" in md   # list, not char-by-char
        assert "src/thing.py" in md                               # deliverable lead
        assert "Round 1 · builder" in md and "Round 2 · reviewer" in md
        assert "(66.7s)" in md                                    # latency rendered
        assert "I implemented the Thing protocol" in md           # real reply prose, not invented
        assert "## The last turn" in md and "## Where it landed" in md

    def test_pending_reads_as_queued(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)
        assert "Queued" in narrative.build(collab, hid).splitlines()[3] or \
               "In progress" in narrative.build(collab, hid)

    def test_done_by_hand_is_not_reported_as_autonomous(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)
        hc.claim(collab, hid)
        hc.done(collab, hid)  # moved to done WITHOUT an autonomous_done event
        md = narrative.build(collab, hid)
        assert "Marked done" in md
        assert "Signed off autonomously: **no**" in md

    def test_autonomous_signoff_is_reported(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        done_ev = {"schema_version": "0.1", "ts": "2026-01-01T00:03:00Z", "run_id": "t",
                   "span_id": "r2:reviewer:signoff", "stage": "autopilot.autonomous_done",
                   "role": "reviewer", "artifact": f"handoff:{hid}",
                   "decision": {"action": "autonomous_done", "reason_codes": [f"done:{hid}"]}}
        _two_turns(collab, hid, extra=[done_ev])
        hc.claim(collab, hid)
        hc.done(collab, hid)
        md = narrative.build(collab, hid)
        assert "shipped autonomously" in md
        assert "Signed off autonomously: **yes**" in md

    def test_capped_run_reads_as_gate_held(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        cap_ev = {"schema_version": "0.1", "ts": "2026-01-01T00:03:00Z", "run_id": "t", "span_id": None,
                  "stage": "autopilot.pause", "role": "autopilot", "artifact": None,
                  "decision": {"action": "cap", "reason_codes": [f"root:{hid}", "outcome:capped"]}}
        _two_turns(collab, hid, extra=[cap_ev])
        md = narrative.build(collab, hid)
        assert "Ran the full round budget" in md and "gate held" in md

    def test_blocker_turn_is_flagged(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid,
                   reviewer_text="I have to stop here and flag a blocker — I can't do this as configured.")
        md = narrative.build(collab, hid)
        assert "⚠ blocker" in md
        assert "Stalled on a blocker" in md

    def test_no_events_still_renders_why_and_landing(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)  # no replies, no events
        md = narrative.build(collab, hid)
        assert "why it matters" in md
        assert "## Where it landed" in md
        assert "## How it unfolded" not in md   # no turns -> section omitted, not faked

    def test_escalation_surfaces_in_narrative(self, tmp_path):
        import escalation
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)
        escalation.write(collab, hid, [{"lane": "clock", "description": "tz bug at gateway.py:233",
                                        "regression_test": "test_skew"}], attempts=1)
        md = narrative.build(collab, hid)
        assert "⚠ Escalated to you" in md
        assert "## ⚠ Needs your fix" in md
        assert f"autopilot/escalations/{hid}.md" in md

    def test_missing_handoff_raises(self, tmp_path):
        collab = str(tmp_path / "c")
        _handoff(collab)
        with pytest.raises(hc.HandoffNotFound):
            narrative.build(collab, "999")


class TestRunSelection:
    """The live run must win over a PRIOR archived run of the same hid — the cross-run bleed that showed
    yesterday's failed turns (mislabeled with today's models) on the dashboard card."""

    def _archived_run(self, collab, hid, uid, reply_text):
        d = Path(collab) / "autopilot" / "history" / uid
        d.mkdir(parents=True, exist_ok=True)
        rel = _reply(collab, f"{uid}-builder.md", reply_text)
        ev = _ev(hid, 1, "builder", rel)
        ev["ts"] = "2026-01-01T00:00:00Z"  # OLD
        (d / "events.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
        (d / "run.json").write_text(json.dumps({"run_uid": uid, "started_ts": "2026-01-01T00:00:00Z"}),
                                    encoding="utf-8")

    def _status(self, collab, started, run_uid):
        p = Path(collab) / "autopilot" / "status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"started_ts": started, "run_uid": run_uid,
                                 "run_seats": {"builder": "gpt-5.6-terra"}}), encoding="utf-8")

    def test_live_run_wins_over_prior_archived_run(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        # a PRIOR archived run of 001 that ended in a blocker
        self._archived_run(collab, hid, "20260101T000000Z-1",
                           "I have to stop and flag a blocker — can't do this as configured.")
        # a LIVE run (newer started_ts) whose builder actually did the work — its turn is in the live log
        self._status(collab, "2026-06-01T00:00:00Z", "20260601T000000Z-2")
        rel = _reply(collab, "live-builder.md", "Implemented the gateway and ran the tests: 78 passed.")
        live_ev = _ev(hid, 1, "builder", rel)
        live_ev["ts"] = "2026-06-01T00:05:00Z"
        _write_events(collab, [live_ev])

        md = narrative.build(collab, hid)
        assert "Implemented the gateway" in md          # the LIVE run's turn
        assert "flag a blocker" not in md               # NOT the prior archived run
        assert "gpt-5.6-terra" in md                    # labeled with the live run's model

    def test_falls_back_to_archived_when_no_live_run(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        self._archived_run(collab, hid, "20260101T000000Z-1", "Implemented it cleanly in the past run.")
        # no status.json -> no live run
        md = narrative.build(collab, hid)
        assert "Implemented it cleanly" in md


class TestWrite:
    def test_write_persists_and_is_idempotent(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)

        sp = narrative.write(collab, hid)
        assert Path(sp).exists() and Path(sp).name == "001.md"
        _, path = hc._reconcile(collab, hid)
        text1 = Path(path).read_text("utf-8")
        assert text1.count("<!-- autopilot-narrative:001 -->") == 1

        narrative.write(collab, hid)  # regenerate — must REPLACE the block, not stack a second one
        text2 = Path(path).read_text("utf-8")
        assert text2.count("<!-- autopilot-narrative:001 -->") == 1
        assert text2.count("<!-- /autopilot-narrative:001 -->") == 1
        # the original handoff content survives intact alongside the appended block
        assert "## Deliverables" in text2
        assert contracts.parse_handoff(Path(path))["frontmatter"]["id"] == "001-thing"

    def test_build_is_read_only(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = _handoff(collab)
        _two_turns(collab, hid)
        _, path = hc._reconcile(collab, hid)
        before = Path(path).read_text("utf-8")
        narrative.build(collab, hid)
        assert Path(path).read_text("utf-8") == before                 # handoff untouched
        assert not (Path(collab) / "autopilot" / "summaries").exists()  # no summaries dir written


class TestUnits:
    def test_as_list_handles_both_spellings(self):
        assert narrative._as_list(["a", "b"]) == ["a", "b"]
        assert narrative._as_list("[PT-1 x, PT-2 y]") == ["PT-1 x", "PT-2 y"]
        assert narrative._as_list("money, auth") == ["money", "auth"]
        assert narrative._as_list(None) == []

    def test_gist_detects_blocker_and_skips_structure(self):
        gist, blk = narrative._gist("## Heading\n\nI have to stop and flag a blocker here.")
        assert blk is True and gist.startswith("I have to stop")
        gist2, blk2 = narrative._gist("Implemented cleanly.\nMore.")
        assert blk2 is False and gist2.startswith("Implemented cleanly")

    def test_gist_empty(self):
        assert narrative._gist("") == ("", False)

    def test_paragraphs_caps_length(self):
        long = "x" * 2000
        out = narrative._paragraphs(long, n=2, limit=620)
        assert len(out) <= 620 and out.endswith("…")

    def test_paragraphs_keeps_two_then_stops(self):
        out = narrative._paragraphs("one\n\ntwo\n\nthree", n=2)
        assert "one" in out and "two" in out and "three" not in out
