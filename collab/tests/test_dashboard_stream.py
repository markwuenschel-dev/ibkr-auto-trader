"""Resumable dashboard snapshot-stream contract."""

from __future__ import annotations

import sys
import time
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import dashboard_core as dc  # noqa: E402
import dashboard_web as dw  # noqa: E402
import handoff_core as hc  # noqa: E402


def test_broker_sequences_only_material_changes_and_stamps_stream_health() -> None:
    snapshots = [
        {"schema_version": "1.0", "ts": "t1", "items": [], "health": {}, "freshness": {}},
        {"schema_version": "1.0", "ts": "t2", "items": [], "health": {}, "freshness": {}},
        {
            "schema_version": "1.0",
            "ts": "t3",
            "items": [{"id": "001", "operational_state": "queued", "freshness": {"age": 1}}],
            "health": {},
            "freshness": {},
        },
    ]
    index = 0

    def read():
        nonlocal index
        value = snapshots[index]
        index = min(index + 1, len(snapshots) - 1)
        return value

    broker = dw._SnapshotBroker(read, instance_id="instance-a")
    first = broker.refresh()
    same = broker.refresh()
    changed = broker.refresh()
    assert first["sequence"] == 1
    assert same["sequence"] == 1
    assert changed["sequence"] == 2
    assert changed["data"]["stream"]["status"] == "connected"
    assert changed["data"]["health"]["stream"]["status"] == "healthy"


def test_broker_resume_replays_available_events_and_reconciles_gaps() -> None:
    state = {"n": 0}
    broker = dw._SnapshotBroker(lambda: {"schema_version": "1.0", "items": [{"id": str(state["n"])}]})
    one = broker.refresh()
    state["n"] = 1
    two = broker.refresh()
    state["n"] = 2
    three = broker.refresh()

    replay = broker.events_after(one["id"])
    assert [event["id"] for event in replay] == [two["id"], three["id"]]
    assert all(event["type"] == "snapshot" for event in replay)
    assert broker.events_after(three["id"]) == []
    assert broker.events_after("another-instance:1")[0]["type"] == "reconcile"
    assert broker.events_after(f"{broker.instance_id}:999")[0]["type"] == "reconcile"


def test_real_source_change_reaches_broker_within_two_seconds(tmp_path: Path) -> None:
    collab = tmp_path / "collab"
    broker = dw._SnapshotBroker(lambda: dc.snapshot(collab))
    broker.refresh()
    started = time.monotonic()
    hid = hc.create(collab, to="reviewer", from_="builder", title="stream", body="x")["id"]
    event = broker.refresh()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert event["data"]["items"][0]["id"] == hid
    assert event["sequence"] == 2
