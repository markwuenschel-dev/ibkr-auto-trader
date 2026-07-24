"""Failure-injection contracts for truthful dashboard observability."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import dashboard_core as dc  # noqa: E402
import dashboard_web as dw  # noqa: E402
import llm_gateway as gw  # noqa: E402
import model_observability as mo  # noqa: E402


def _event(
    state: mo.LifecycleState,
    *,
    event_id: str,
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    timestamp: str = "2026-07-22T12:00:00Z",
) -> mo.ModelAttemptEvent:
    return mo.ModelAttemptEvent(
        event_id=event_id,
        attempt_id=attempt_id,
        request_id=request_id,
        run_uid="failure-run",
        seat="builder",
        requested_model="gpt-5.6-luna",
        state=state,
        event_ts=timestamp,
        attempt_number=1,
        source="gateway_client",
    )


def test_broker_evicts_old_snapshots_and_reconciles_a_stale_cursor() -> None:
    state = {"value": 0}
    broker = dw._SnapshotBroker(
        lambda: {"schema_version": "1.0", "value": state["value"]},
        instance_id="bounded-broker",
        capacity=3,
    )
    first = broker.refresh()
    for value in range(1, 7):
        state["value"] = value
        broker.refresh()

    assert len(broker._events) == 3
    replay = broker.events_after(first["id"])
    assert len(replay) == 1
    assert replay[0]["type"] == "reconcile"
    assert replay[0]["data"]["value"] == 6


def test_timeout_cancellation_and_retry_are_distinct_lifecycle_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event_log = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(event_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    config = gw.GatewayConfig.from_env()

    def invoke(exc: Exception, *, retry: int = 0) -> None:
        metadata = gw.request_metadata("gpt-5.6-luna", feature="failure-test")
        metadata["retry"] = retry
        payload = {"model": "gpt-5.6-luna", "input": "private", "metadata": metadata}
        with pytest.raises(type(exc)):
            gw.post_json(
                config.url("responses"),
                config.virtual_key,
                payload,
                1,
                opener=lambda *args, **kwargs: (_ for _ in ()).throw(exc),
            )

    invoke(TimeoutError("late"))
    invoke(InterruptedError("operator cancelled"))
    invoke(TimeoutError("retry late"), retry=1)

    reduced = mo.reduce_attempts(mo.read_events(event_log))
    assert [attempt["state"] for attempt in reduced] == ["timed_out", "cancelled", "timed_out"]
    assert reduced[2]["states"][:2] == ["retrying", "connecting"]
    assert all(not attempt["conflicts"] for attempt in reduced)


def test_model_event_persistence_failure_is_atomic_and_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(path))
    event = _event("connecting", event_id="event-1")

    def fail_append(_path: Path, _event: mo.ModelAttemptEvent) -> str:
        raise OSError("disk unavailable")

    monkeypatch.setattr(mo, "append_event", fail_append)
    with pytest.raises(mo.ModelObservabilityError, match="persistence failed"):
        mo.append_if_configured(event)

    health = mo.read_persistence_health(path)
    assert health["status"] == "unavailable"
    assert health["failed_event_id"] == "event-1"
    assert "disk unavailable" not in json.dumps(health)


def test_redacted_call_ledger_failure_has_a_distinct_visible_health_dimension(
    tmp_path: Path,
) -> None:
    health_path = tmp_path / "autopilot" / "model-calls-health.json"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "unavailable",
                "reason": "PermissionError: telemetry append failed",
                "telemetry_failures": 2,
                "updated_ts": "2026-07-22T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    health = dc._snapshot_health(
        tmp_path, items=[], model_activity=[], live=True, run_uid=None, ts="now"
    )
    assert health["call_ledger_persistence"] == {
        "status": "unavailable",
        "updated_ts": "2026-07-22T12:00:00Z",
        "reason": "PermissionError: telemetry append failed",
    }


def test_missing_or_failed_telemetry_never_projects_as_healthy() -> None:
    completed = mo.reduce_attempts(
        [
            _event("connecting", event_id="event-1"),
            _event("completed", event_id="event-2", timestamp="2026-07-22T12:00:01Z"),
        ]
    )
    unknown = dc._snapshot_health(
        "unused",
        items=[],
        model_activity=completed,
        live=True,
        run_uid="failure-run",
        ts="now",
    )
    assert unknown["langfuse"]["status"] == "unknown"

    failed = mo.reduce_attempts(
        [
            _event("connecting", event_id="event-1"),
            _event("completed", event_id="event-2", timestamp="2026-07-22T12:00:01Z"),
            mo.ModelAttemptEvent(
                **{
                    **_event(
                        "telemetry_failed",
                        event_id="event-3",
                        timestamp="2026-07-22T12:00:10Z",
                    ).to_dict(),
                    "source": "langfuse_reconciler",
                    "telemetry_result": "missing",
                }
            ),
        ]
    )
    unavailable = dc._snapshot_health(
        "unused",
        items=[],
        model_activity=failed,
        live=True,
        run_uid="failure-run",
        ts="now",
    )
    assert unavailable["langfuse"]["status"] == "unavailable"


def test_active_run_does_not_inherit_prior_run_persistence_health(tmp_path: Path) -> None:
    autopilot = tmp_path / "autopilot"
    autopilot.mkdir()
    for name, record_type in (
        ("run-history-health.json", "run_history_health"),
        ("model-observability-health.json", "model_observability_health"),
        ("model-calls-health.json", "model_call_ledger_health"),
        ("run-events-health.json", "run_evidence_health"),
    ):
        (autopilot / name).write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "record_type": record_type,
                    "run_uid": "prior-run",
                    "status": "healthy",
                    "updated_ts": "2026-07-22T12:00:00Z",
                    "reason": None,
                }
            ),
            encoding="utf-8",
        )

    health = dc._snapshot_health(
        tmp_path,
        items=[],
        model_activity=[],
        live=True,
        run_uid="active-run",
        ts="2026-07-22T12:05:00Z",
    )

    for dimension in (
        "run_archive_persistence",
        "attempt_persistence",
        "call_ledger_persistence",
        "run_evidence_persistence",
    ):
        assert health[dimension]["status"] == "unknown"
        assert "active run" in health[dimension]["reason"]
