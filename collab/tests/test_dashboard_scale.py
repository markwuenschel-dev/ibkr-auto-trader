"""Measured local scale budgets for the collab dashboard projection."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import dashboard_core as dc  # noqa: E402
import dashboard_web as dw  # noqa: E402
import model_observability as mo  # noqa: E402
import run_projection as rp  # noqa: E402

TEN_K_PROJECTION_SECONDS = 3.0
TEN_K_CACHED_READ_SECONDS = 0.25
LIVE_ATTEMPT_WINDOW = 500


def _model_events(count: int) -> list[mo.ModelAttemptEvent]:
    events = []
    for index in range(count // 2):
        attempt = f"attempt-{index:05d}"
        request = f"request-{index:05d}"
        for offset, state in enumerate(("connecting", "completed")):
            events.append(
                mo.ModelAttemptEvent(
                    event_id=f"event-{index:05d}-{offset}",
                    attempt_id=attempt,
                    request_id=request,
                    run_uid="scale-run",
                    seat=f"seat-{index % 32:02d}",
                    requested_model=f"model-{index % 32:02d}",
                    state=state,
                    event_ts=f"2026-07-22T12:{index % 60:02d}:{offset:02d}Z",
                    attempt_number=1,
                    source="gateway_client",
                )
            )
    return events


def test_ten_thousand_model_events_project_within_local_budget() -> None:
    events = _model_events(10_000)
    started = time.perf_counter()
    projection = rp.project(
        run_uid="scale-run",
        plan=None,
        model_events=events,
        decisions=[],
    )
    elapsed = time.perf_counter() - started

    assert len(projection["attempts"]) == 5_000
    assert elapsed < TEN_K_PROJECTION_SECONDS


def test_live_snapshot_collections_are_explicitly_windowed() -> None:
    attempts = mo.reduce_attempts(_model_events(10_000))
    windowed, windows = dc._window_live_projection(
        {
            "attempts": attempts,
            "candidates": [{"candidate_id": str(i)} for i in range(800)],
            "validations": [{"validation_id": str(i)} for i in range(800)],
            "requirements": [{"requirement_id": str(i)} for i in range(800)],
            "dispositions": [{"candidate_id": str(i)} for i in range(800)],
        }
    )

    assert len(windowed["attempts"]) == LIVE_ATTEMPT_WINDOW
    assert windows["model_activity"] == {
        "total": 5_000,
        "returned": LIVE_ATTEMPT_WINDOW,
        "truncated": True,
        "order": "latest",
    }
    assert windows["provider_attempts"]["total"] == 5_000
    for key in ("candidates", "validations", "requirements", "dispositions"):
        assert windows[key]["total"] == 800
        assert windows[key]["returned"] < 800
        assert windows[key]["truncated"] is True


def test_ten_thousand_trace_events_have_bounded_snapshot_payload_and_cached_read(
    tmp_path: Path,
) -> None:
    collab = tmp_path / "collab"
    log = collab / "logs" / "events.jsonl"
    log.parent.mkdir(parents=True)
    rows = [
        {
            "ts": f"2026-07-22T12:{index % 60:02d}:00Z",
            "stage": "autopilot.round",
            "role": f"seat-{index % 32:02d}",
            "artifact": f"handoff:{index % 100:03d}",
            "decision": {"action": "turn"},
            "metrics": {"latency_ms": index % 1000, "resp_bytes": 20},
        }
        for index in range(10_000)
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    first = dc.read_events(collab)
    started = time.perf_counter()
    second = dc.read_events(collab)
    cached_elapsed = time.perf_counter() - started
    stats = dc.run_stats(second)
    broker = dw._SnapshotBroker(
        lambda: {
            "schema_version": "1.0",
            "events": second[-60:],
            "stats": stats,
        }
    )
    payload = broker.refresh()["data"]

    assert len(first) == len(second) == 10_000
    assert cached_elapsed < TEN_K_CACHED_READ_SECONDS
    assert len(payload["events"]) == 60
    assert len(payload["stats"]["latency_series"]) == 40
    assert len(json.dumps(payload)) < 250_000
    assert len(broker._events) <= 512
