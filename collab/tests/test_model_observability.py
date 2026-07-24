"""Immutable, redacted model-attempt lifecycle evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import model_observability as mo  # noqa: E402


def _event(state: str, *, event_id: str, ts: str) -> mo.ModelAttemptEvent:
    return mo.ModelAttemptEvent(
        event_id=event_id,
        attempt_id="attempt-1",
        request_id="request-1",
        run_uid="run-1",
        seat="builder",
        requested_model="gpt-5.6-luna",
        state=state,
        event_ts=ts,
        attempt_number=1,
        source="gateway_client",
    )


def test_append_is_idempotent_and_conflicting_event_id_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    first = _event("queued", event_id="event-1", ts="2026-07-22T00:00:00Z")
    assert mo.append_event(path, first) == "appended"
    assert mo.append_event(path, first) == "duplicate"

    conflict = _event("starting", event_id="event-1", ts="2026-07-22T00:00:01Z")
    with pytest.raises(mo.ModelObservabilityError, match="different content"):
        mo.append_event(path, conflict)

    assert len(path.read_text("utf-8").splitlines()) == 1


def test_reduce_orders_lifecycle_and_reports_illegal_transition() -> None:
    events = [
        _event("completed", event_id="event-3", ts="2026-07-22T00:00:03Z"),
        _event("queued", event_id="event-1", ts="2026-07-22T00:00:00Z"),
        _event("connecting", event_id="event-2", ts="2026-07-22T00:00:02Z"),
    ]
    attempt = mo.reduce_attempts(events)[0]
    assert attempt["state"] == "completed"
    assert attempt["states"] == ["queued", "connecting", "completed"]
    assert attempt["conflicts"] == []

    invalid = [*events, _event("generating", event_id="event-4", ts="2026-07-22T00:00:04Z")]
    attempt = mo.reduce_attempts(invalid)[0]
    assert attempt["state"] == "completed"
    assert attempt["conflicts"][0]["kind"] == "event_after_terminal"


def test_event_rejects_prompt_output_reasoning_and_credentials() -> None:
    for forbidden in ("prompt", "output", "chain_of_thought", "authorization", "api_key"):
        with pytest.raises(mo.ModelObservabilityError, match="forbidden detail field"):
            mo.ModelAttemptEvent(
                event_id=f"event-{forbidden}",
                attempt_id="attempt-1",
                request_id="request-1",
                run_uid="run-1",
                seat="builder",
                requested_model="gpt-5.6-luna",
                state="connecting",
                event_ts="2026-07-22T00:00:00Z",
                attempt_number=1,
                source="gateway_client",
                detail={forbidden: "secret"},
            )


def test_read_skips_torn_tail_but_rejects_non_tail_corruption(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    path.write_text(
        json.dumps(_event("queued", event_id="event-1", ts="2026-07-22T00:00:00Z").to_dict())
        + "\n"
        + '{"torn":',
        encoding="utf-8",
    )
    assert [event.state for event in mo.read_events(path)] == ["queued"]

    path.write_text('{"bad":\n{}\n', encoding="utf-8")
    with pytest.raises(mo.ModelObservabilityError, match="invalid JSON"):
        mo.read_events(path)


def test_append_refuses_torn_tail_instead_of_making_corruption_interior(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    path.write_text('{"torn":', encoding="utf-8")
    with pytest.raises(mo.ModelObservabilityError, match="torn tail"):
        mo.append_event(path, _event("queued", event_id="event-1", ts="2026-07-22T00:00:00Z"))
    assert path.read_text("utf-8") == '{"torn":'


def test_health_aware_append_records_a_durable_visible_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "model-events.jsonl"

    def fail_append(*args, **kwargs):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(mo, "append_event", fail_append)
    with pytest.raises(mo.ModelObservabilityError, match="model-attempt persistence failed"):
        mo.append_with_health(
            path,
            _event("queued", event_id="event-1", ts="2026-07-22T00:00:00Z"),
        )

    health = mo.read_persistence_health(path)
    assert health["status"] == "unavailable"
    assert health["failed_event_id"] == "event-1"
    assert health["run_uid"] == "run-1"
    assert health["reason"] == "OSError: model-attempt evidence append failed"
    assert str(tmp_path) not in health["reason"]


def test_equal_timestamps_preserve_append_order() -> None:
    events = [
        _event("queued", event_id="z-event", ts="2026-07-22T00:00:00Z"),
        _event("connecting", event_id="a-event", ts="2026-07-22T00:00:00Z"),
        _event("completed", event_id="m-event", ts="2026-07-22T00:00:00Z"),
    ]
    assert mo.reduce_attempts(events)[0]["states"] == ["queued", "connecting", "completed"]


def test_reducer_preserves_operator_safe_transport_performance_and_progress_fields() -> None:
    started = _event("connecting", event_id="event-1", ts="2026-07-22T00:00:00Z")
    completed = mo.ModelAttemptEvent(
        **{
            **_event("completed", event_id="event-2", ts="2026-07-22T00:00:03Z").to_dict(),
            "gateway_route": "responses",
            "gateway_request_id": "litellm-1",
            "provider_request_id": "provider-1",
            "actual_model": "openai/gpt-5.6-luna-2026-07-01",
            "provider": "openai",
            "completion_status": "completed",
            "first_token_latency_ms": 120.0,
            "total_duration_ms": 3000.0,
            "streaming": True,
            "tokens": {"input": 10, "output": 20, "cached": 4, "total": 30},
            "cost": 0.02,
            "retry_count": 1,
            "detail": {"phase": "response_complete", "last_chunk_ts": "2026-07-22T00:00:02Z"},
        }
    )
    attempt = mo.reduce_attempts([started, completed])[0]
    assert attempt["completed_ts"] == "2026-07-22T00:00:03Z"
    assert attempt["gateway_route"] == "responses"
    assert attempt["gateway_request_id"] == "litellm-1"
    assert attempt["provider_request_id"] == "provider-1"
    assert attempt["first_token_latency_ms"] == 120.0
    assert attempt["total_duration_ms"] == 3000.0
    assert attempt["streaming"] is True
    assert attempt["tokens"] == {"input": 10, "output": 20, "cached": 4, "total": 30}
    assert attempt["cost"] == 0.02
    assert attempt["retry_count"] == 1
    assert attempt["last_activity"] == {
        "phase": "response_complete",
        "last_chunk_ts": "2026-07-22T00:00:02Z",
    }
