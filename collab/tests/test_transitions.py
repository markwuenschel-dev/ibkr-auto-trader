"""A human override must stay possible, and must never wear the autonomous label.

Regression for the 2026-07-15 audit. ``hc.done(collab, hid)`` took no provenance and wrote none: the
file in ``done/`` was byte-identical whether the 11-condition contract closed it or a human clicked
Approve. Three of four production callers reached ``done/`` with no contract at all, and the only trace
of which had happened was a best-effort ``_emit_safe`` log line written AFTER the CAS — so a dropped
emit turned an autonomous close into a human one, and nothing on the artifact distinguished them.

Two properties, and they pull in opposite directions on purpose:

  * an operator can ALWAYS close a handoff by hand — that capability is not a bug to be removed;
  * no human override may ever be reported as verified, by any surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import closeout_report as cr  # noqa: E402
import collab_common as cc  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402
import narrative  # noqa: E402
import transitions as tr  # noqa: E402


def _claimed(tmp_path):
    collab = str(tmp_path / "c")
    hc.create(collab, to="reviewer", from_="builder", title="x", body="y")
    hc.claim(collab, "001")
    return collab


# --------------------------------------------------------------------------- #
# the primitive refuses to close anonymously
# --------------------------------------------------------------------------- #


def test_done_requires_a_transition_kind(tmp_path):
    collab = _claimed(tmp_path)
    with pytest.raises(TypeError):
        hc.done(collab, "001")  # type: ignore[call-arg]  # the missing kind is the point
    assert hc.state_of(collab, "001") == "claimed", "a refused close must not move the file"


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"kind": "verified", "actor": "a"}, "an invented kind"),
        ({"kind": None, "actor": "a"}, "no kind"),
        ({"kind": tr.KIND_HUMAN, "actor": ""}, "no actor"),
        ({"kind": tr.KIND_HUMAN, "actor": "a"}, "a human override with no reason"),
        ({"kind": tr.KIND_HUMAN, "actor": "a", "reason": "   "}, "a whitespace reason"),
        ({"kind": tr.KIND_AUTONOMOUS, "actor": "a"}, "an autonomous close with no receipt"),
        ({"kind": tr.KIND_AUTONOMOUS, "actor": "a", "receipt": ""}, "an empty receipt"),
    ],
)
def test_done_refuses_a_dishonest_transition_and_does_not_move_the_file(tmp_path, kw, why):
    collab = _claimed(tmp_path)
    with pytest.raises(cc.CollabError):
        hc.done(collab, "001", **kw)
    assert hc.state_of(collab, "001") == "claimed", f"{why} must not close the handoff"


# --------------------------------------------------------------------------- #
# human override: always possible, never labelled verified
# --------------------------------------------------------------------------- #


def test_a_human_override_still_closes_the_handoff(tmp_path):
    """The capability itself. An operator must be able to close what the machinery cannot."""
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=tr.KIND_HUMAN, actor="mark", reason="gate is wrong; shipping by hand")
    assert hc.state_of(collab, "001") == "done"


def test_a_human_override_is_never_autonomous(tmp_path):
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=tr.KIND_HUMAN, actor="mark", reason="shipping by hand")
    rec = tr.read(collab, "001")
    assert rec["kind"] == tr.KIND_HUMAN
    assert rec["actor"] == "mark"
    assert rec["reason"] == "shipping by hand"
    assert rec["receipt"] is None
    assert rec["ts"], "a transition must be timestamped"
    assert tr.is_autonomous(rec) is False
    assert tr.is_human_override(rec) is True


def test_a_human_override_cannot_be_forged_autonomous_by_flipping_the_kind(tmp_path):
    """is_autonomous demands the receipt too: the kind alone is a claim, not evidence."""
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=tr.KIND_HUMAN, actor="mark", reason="by hand")
    rec = tr.read(collab, "001")
    assert tr.is_autonomous({**rec, "kind": tr.KIND_AUTONOMOUS}) is False


def test_an_autonomous_close_records_its_receipt(tmp_path):
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=tr.KIND_AUTONOMOUS, actor="reviewer", receipt="a" * 64,
            candidate_id="cand-1")
    rec = tr.read(collab, "001")
    assert tr.is_autonomous(rec) is True
    assert rec["receipt"] == "a" * 64
    assert rec["candidate_id"] == "cand-1"
    assert rec["reason"] is None


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        (tr.KIND_HUMAN, {"actor": "mark", "reason": "by hand"}),
        (tr.KIND_AUTONOMOUS, {"actor": "reviewer", "receipt": "h" * 64}),
    ],
)
def test_the_two_kinds_never_share_a_label(tmp_path, kind, kwargs):
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=kind, **kwargs)
    label = tr.label_of(tr.read(collab, "001"))
    if kind == tr.KIND_HUMAN:
        assert label == tr.LABEL_HUMAN
        for forbidden in ("AUTONOMOUS", "VERIFIED", "GREEN", "checkout passed"):
            assert forbidden not in label, f"a human override must never say {forbidden!r}"
    else:
        assert label == tr.LABEL_AUTONOMOUS
    assert tr.LABEL_HUMAN != tr.LABEL_AUTONOMOUS


# --------------------------------------------------------------------------- #
# fail-closed: unknown provenance is not verified
# --------------------------------------------------------------------------- #


def test_an_absent_record_is_unrecorded_not_autonomous(tmp_path):
    """A crash between the CAS and the record write must not read as verified."""
    collab = _claimed(tmp_path)
    assert tr.read(collab, "001") is None
    assert tr.is_autonomous(None) is False
    assert tr.label_of(None) == tr.LABEL_UNRECORDED
    assert tr.summary(collab, "001")["autonomous"] is False


def test_a_corrupt_record_is_unrecorded_not_autonomous(tmp_path):
    collab = _claimed(tmp_path)
    hc.done(collab, "001", kind=tr.KIND_AUTONOMOUS, actor="r", receipt="h" * 64)
    tr._path(collab, "001").write_text("{not json", encoding="utf-8")
    assert tr.read(collab, "001") is None
    assert tr.summary(collab, "001")["label"] == tr.LABEL_UNRECORDED


# --------------------------------------------------------------------------- #
# the rendering surfaces
# --------------------------------------------------------------------------- #


def test_the_dashboard_never_shows_a_human_override_as_autonomous(tmp_path):
    collab = _claimed(tmp_path)
    dc.advance_handoff(collab, "001", actor="mark", reason="shipping by hand")
    row = next(r for r in dc.board(collab)["done"] if r["id"] == "001")
    assert row["closed_by"] == tr.KIND_HUMAN
    assert row["closed_autonomously"] is False
    assert row["closed_actor"] == "mark"
    assert row["closed_reason"] == "shipping by hand"
    assert "OVERRIDE" in row["closed_label"]
    for forbidden in ("AUTONOMOUS", "VERIFIED", "GREEN"):
        assert forbidden not in row["closed_label"]


def test_the_dashboard_approve_route_requires_an_actor_and_a_reason(tmp_path):
    collab = _claimed(tmp_path)
    with pytest.raises(TypeError):
        dc.advance_handoff(collab, "001")  # type: ignore[call-arg]
    with pytest.raises(cc.CollabError):
        dc.advance_handoff(collab, "001", actor="mark", reason="")
    assert hc.state_of(collab, "001") == "claimed"


def test_the_closeout_report_names_the_override_and_denies_it_the_verified_label(tmp_path):
    collab = _claimed(tmp_path)
    dc.advance_handoff(collab, "001", actor="mark", reason="gate is wrong")
    summary = cr.collect(collab, "001")
    assert summary["closed_autonomously"] is False
    assert summary["transition"]["human_override"] is True
    md = cr.render_markdown(summary)
    assert "HUMAN OVERRIDE" in md
    assert "mark" in md and "gate is wrong" in md
    assert "no authoritative verification receipt" in md


def test_the_narrative_names_the_override(tmp_path):
    collab = _claimed(tmp_path)
    dc.advance_handoff(collab, "001", actor="mark", reason="gate is wrong")
    d = narrative.collect(collab, "001")
    assert d["transition"]["human_override"] is True
    md = narrative.render_markdown(d)
    assert "Human override" in md or "HUMAN OVERRIDE" in md
    assert "not** backed by an authoritative verification receipt" in md
