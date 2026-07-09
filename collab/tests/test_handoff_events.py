"""Tests for handoff_events (Workstream A: self-logging handoff lifecycle). stdlib only.

Each test emits real JSONL through tools/lib/trace.py into a temp-dir log, reads it back, and
asserts the envelope shape (stage/role/decision.action/reason_codes/artifact/schema_version) per
ARCHITECTURE.md §7.1 (handoff lifecycle) + §8 ("log first").

Run:  python -m pytest tests/test_handoff_events.py -v   (from the collab-kit repo root)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the lifecycle helper + emitter importable whether run from repo root or elsewhere.
_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import handoff_events as he  # noqa: E402

RUN_ID = "test-handoff-lifecycle"
HID = "042-widget"


def _log(tmp_path) -> str:
    return str(tmp_path / "trace.jsonl")


def _read(path: str) -> list[dict]:
    """Read a JSONL log back into a list of dicts; every line must parse independently (§8)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --------------------------------------------------------------------------- #
# Per-transition envelope shape
# --------------------------------------------------------------------------- #


def test_on_create(tmp_path):
    log = _log(tmp_path)
    ev = he.on_create(log, RUN_ID, HID, span_id="h1", title="widget-core")
    (rec,) = _read(log)
    assert rec == ev  # returned dict == the line written
    assert rec["stage"] == "handoff.create"
    assert rec["role"] == "builder"
    assert rec["artifact"] == f"handoff:{HID}"
    assert rec["decision"]["action"] == "handoff"
    assert rec["decision"]["reason_codes"] == ["propose:widget-core"]
    assert rec["schema_version"]  # present + truthy
    assert rec["parent_span_id"] is None  # root of the span tree


def test_on_claim(tmp_path):
    log = _log(tmp_path)
    ev = he.on_claim(log, RUN_ID, HID, span_id="h2", parent_span_id="h1", by="reviewer")
    (rec,) = _read(log)
    assert rec == ev
    assert rec["stage"] == "review"
    assert rec["role"] == "reviewer"
    assert rec["artifact"] == f"handoff:{HID}"
    assert rec["decision"]["action"] == "route"
    assert rec["decision"]["reason_codes"] == ["claim:reviewer"]
    assert rec["parent_span_id"] == "h1"
    assert rec["schema_version"]


def test_on_review_accept(tmp_path):
    log = _log(tmp_path)
    codes = ["amendments-landed", "implementation-authorized"]
    he.on_review(
        log, RUN_ID, HID, span_id="h3", parent_span_id="h2",
        verdict="approved", reason_codes=codes,
    )
    (rec,) = _read(log)
    assert rec["stage"] == "review"
    assert rec["role"] == "reviewer"
    assert rec["decision"]["action"] == "accept"  # approved -> accept
    assert rec["decision"]["reason_codes"] == codes
    assert rec["eval"]["verdict"] == "approved"
    assert rec["artifact"] == f"handoff:{HID}"
    assert rec["schema_version"]


def test_on_review_revise(tmp_path):
    log = _log(tmp_path)
    codes = ["lock-semantics-underspecified", "path-edge-cases"]
    he.on_review(
        log, RUN_ID, HID, span_id="h3", parent_span_id="h2",
        verdict="conditional_approval", reason_codes=codes,
    )
    (rec,) = _read(log)
    assert rec["decision"]["action"] == "revise"  # conditional -> revise
    assert rec["decision"]["reason_codes"] == codes
    assert rec["eval"]["verdict"] == "conditional_approval"


def test_verdict_mapping():
    assert he._verdict_to_action("approved") == "accept"
    assert he._verdict_to_action("authorized_to_implement") == "accept"
    assert he._verdict_to_action("conditional_approval") == "revise"
    assert he._verdict_to_action("blocked_on_one_change") == "revise"


def test_unknown_verdict_raises():
    import pytest

    with pytest.raises(ValueError):
        he._verdict_to_action("maybe-later")


def test_on_revise(tmp_path):
    log = _log(tmp_path)
    codes = ["race-safe-rename-break", "path-table"]
    he.on_revise(
        log, RUN_ID, HID, span_id="h4", parent_span_id="h3",
        revision="rev2", reason_codes=codes,
    )
    (rec,) = _read(log)
    assert rec["stage"] == "handoff.revise"
    assert rec["role"] == "builder"
    assert rec["decision"]["action"] == "revise"
    assert rec["decision"]["reason_codes"] == codes
    assert rec["artifact_version"] == "rev2"
    assert rec["artifact"] == f"handoff:{HID}"
    assert rec["schema_version"]


def test_on_done(tmp_path):
    log = _log(tmp_path)
    ev = he.on_done(log, RUN_ID, HID, span_id="h6", parent_span_id="h5")
    (rec,) = _read(log)
    assert rec == ev
    assert rec["stage"] == "handoff.done"
    assert rec["role"] == "builder"
    assert rec["decision"]["action"] == "accept"
    assert rec["decision"]["reason_codes"] == ["claimed->done"]
    assert rec["failure"] is None
    assert rec["artifact"] == f"handoff:{HID}"
    assert rec["schema_version"]


# --------------------------------------------------------------------------- #
# Append semantics + full lifecycle ordering
# --------------------------------------------------------------------------- #


def test_n_calls_produce_n_parseable_lines(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        he.on_claim(log, RUN_ID, HID, span_id=f"c{i}", parent_span_id="h1", by="reviewer")
    recs = _read(log)  # must be exactly 5 independently-parseable lines
    assert len(recs) == 5
    assert [r["span_id"] for r in recs] == [f"c{i}" for i in range(5)]


def test_full_lifecycle_ordered_stages(tmp_path):
    log = _log(tmp_path)
    # create -> claim -> review(revise) -> revise -> review(accept) -> done
    he.on_create(log, RUN_ID, HID, span_id="s1", title="widget-core")
    he.on_claim(log, RUN_ID, HID, span_id="s2", parent_span_id="s1", by="reviewer")
    he.on_review(
        log, RUN_ID, HID, span_id="s3", parent_span_id="s2",
        verdict="conditional_approval", reason_codes=["needs-tests"],
    )
    he.on_revise(
        log, RUN_ID, HID, span_id="s4", parent_span_id="s3",
        revision="rev2", reason_codes=["added-tests"],
    )
    he.on_review(
        log, RUN_ID, HID, span_id="s5", parent_span_id="s4",
        verdict="approved", reason_codes=["all-clear"],
    )
    he.on_done(log, RUN_ID, HID, span_id="s6", parent_span_id="s5")

    recs = _read(log)
    assert [r["stage"] for r in recs] == [
        "handoff.create",
        "review",
        "review",
        "handoff.revise",
        "review",
        "handoff.done",
    ]
    # Verdict edges produced the right derived actions.
    assert recs[2]["decision"]["action"] == "revise"   # conditional
    assert recs[4]["decision"]["action"] == "accept"   # approved
    # Every event is about the same handoff and carries a schema_version.
    assert all(r["artifact"] == f"handoff:{HID}" for r in recs)
    assert all(r["schema_version"] for r in recs)
    # Span tree is a chain: each parent is the previous event's span.
    spans = [r["span_id"] for r in recs]
    parents = [r["parent_span_id"] for r in recs]
    assert parents == [None] + spans[:-1]
