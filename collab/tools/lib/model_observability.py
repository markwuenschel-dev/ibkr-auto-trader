"""Immutable, redacted model-attempt lifecycle evidence and reduction."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import collab_common as cc

SCHEMA_VERSION = "2.0"
RECORD_TYPE = "model_attempt_event"

LifecycleState = Literal[
    "queued",
    "starting",
    "connecting",
    "gateway_accepted",
    "generating",
    "streaming",
    "waiting_for_tool",
    "waiting_for_evaluator",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "retrying",
    "skipped",
    "rejected",
    "accepted",
    "superseded",
    "telemetry_verified",
    "telemetry_failed",
]

_STATES = {
    "queued",
    "starting",
    "connecting",
    "gateway_accepted",
    "generating",
    "streaming",
    "waiting_for_tool",
    "waiting_for_evaluator",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "retrying",
    "skipped",
    "rejected",
    "accepted",
    "superseded",
    "telemetry_verified",
    "telemetry_failed",
}
_EXECUTION_TERMINAL = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "skipped",
    "rejected",
    "accepted",
    "superseded",
}
_TELEMETRY_STATES = {"telemetry_verified", "telemetry_failed"}
_FORBIDDEN_DETAIL_FRAGMENTS = {
    "api_key",
    "authorization",
    "chain_of_thought",
    "completion",
    "content",
    "input",
    "messages",
    "output",
    "prompt",
    "reasoning",
    "secret",
}
_EVENT_INDEX_CACHE: dict[str, tuple[int, int, dict[str, str]]] = {}
_EVENT_INDEX_LOCK = threading.Lock()


class ModelObservabilityError(cc.CollabError):
    """Lifecycle evidence is unsafe, malformed, or internally inconsistent."""


def _validate_detail(value: Any, *, path: str = "detail") -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_detail(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(fragment in normalized for fragment in _FORBIDDEN_DETAIL_FRAGMENTS):
                raise ModelObservabilityError(f"forbidden detail field: {path}.{key}")
            _validate_detail(item, path=f"{path}.{key}")
        return
    raise ModelObservabilityError(f"unsupported detail value at {path}: {type(value).__name__}")


@dataclass(frozen=True)
class ModelAttemptEvent:
    event_id: str
    attempt_id: str
    request_id: str
    run_uid: str | None
    seat: str | None
    requested_model: str | None
    state: LifecycleState
    event_ts: str
    attempt_number: int
    source: str
    schema_version: str = SCHEMA_VERSION
    record_type: str = RECORD_TYPE
    parent_attempt_id: str | None = None
    handoff_id: str | None = None
    candidate_id: str | None = None
    escalation_id: str | None = None
    gateway_route: str | None = None
    gateway_request_id: str | None = None
    provider_request_id: str | None = None
    actual_model: str | None = None
    provider: str | None = None
    completion_status: str | None = None
    failure_classification: str | None = None
    first_token_latency_ms: float | None = None
    total_duration_ms: float | None = None
    streaming: bool | None = None
    tokens: dict[str, int | None] | None = None
    cost: float | None = None
    retry_count: int = 0
    telemetry_result: str | None = None
    observation_id: str | None = None
    trace_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "attempt_id", "request_id", "event_ts", "source"):
            if not str(getattr(self, name) or "").strip():
                raise ModelObservabilityError(f"{name} is required")
        if self.schema_version != SCHEMA_VERSION or self.record_type != RECORD_TYPE:
            raise ModelObservabilityError("unsupported model-attempt event schema")
        if self.state not in _STATES:
            raise ModelObservabilityError(f"unsupported lifecycle state: {self.state!r}")
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ModelObservabilityError("attempt_number must be a positive integer")
        if isinstance(self.retry_count, bool) or self.retry_count < 0:
            raise ModelObservabilityError("retry_count must be a non-negative integer")
        _validate_detail(self.detail)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ModelAttemptEvent:
        try:
            return cls(**value)
        except TypeError as exc:
            raise ModelObservabilityError(f"invalid model-attempt event fields: {exc}") from exc


def event_log_path() -> Path | None:
    explicit = (os.environ.get("COLLAB_MODEL_EVENT_LOG") or "").strip()
    if explicit:
        return Path(explicit)
    call_log = (os.environ.get("COLLAB_MODEL_CALL_LOG") or "").strip()
    if call_log:
        return Path(call_log).with_name("model-events.jsonl")
    collab = (os.environ.get("COLLAB_DIR") or "").strip()
    return Path(collab) / "autopilot" / "model-events.jsonl" if collab else None


def persistence_health_path(path: Path) -> Path:
    """Return the atomic health sidecar for the immutable attempt ledger."""
    return path.with_name("model-observability-health.json")


def _write_persistence_health(
    path: Path,
    status: Literal["healthy", "unavailable"],
    *,
    failed_event_id: str | None = None,
    failure_class: str | None = None,
    run_uid: str | None = None,
) -> None:
    record = {
        "schema_version": "1.0",
        "record_type": "model_observability_health",
        "status": status,
        "updated_ts": now_utc(),
        "reason": (
            f"{failure_class or 'unknown_error'}: model-attempt evidence append failed"
            if status == "unavailable"
            else None
        ),
        "failed_event_id": failed_event_id,
        "run_uid": run_uid,
    }
    health_path = persistence_health_path(path)
    health_path.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(
        health_path,
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
    )


def read_persistence_health(path: Path) -> dict[str, Any]:
    """Read attempt-ledger persistence health without treating absence as healthy."""
    try:
        value = json.loads(persistence_health_path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {
            "status": "unknown",
            "updated_ts": None,
            "reason": "model-attempt persistence has not reported",
            "failed_event_id": None,
        }
    if not isinstance(value, dict):
        return {
            "status": "unavailable",
            "updated_ts": None,
            "reason": "model-attempt persistence health is malformed",
            "failed_event_id": None,
        }
    return value


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _event_index(path: Path) -> dict[str, str]:
    """Return an mtime/size-bound event-id index, rebuilding after external writes."""
    stat = path.stat()
    key = str(path.resolve())
    signature = (stat.st_mtime_ns, stat.st_size)
    with _EVENT_INDEX_LOCK:
        cached = _EVENT_INDEX_CACHE.get(key)
    if cached is not None and cached[:2] == signature:
        return cached[2]
    index = {event.event_id: _canonical(event.to_dict()) for event in read_events(path)}
    with _EVENT_INDEX_LOCK:
        _EVENT_INDEX_CACHE[key] = (signature[0], signature[1], index)
    return index


def _tail_needs_newline(path: Path) -> bool:
    """Validate only a non-newline-terminated tail; never reread the whole ledger."""
    size = path.stat().st_size
    if size == 0:
        return False
    with path.open("rb") as stream:
        stream.seek(max(0, size - 65_536))
        tail = stream.read()
    if tail.endswith(b"\n"):
        return False
    raw_last = tail.splitlines()[-1]
    try:
        json.loads(raw_last.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelObservabilityError(f"torn tail prevents append to {path}") from exc
    return True


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def read_events(path: Path) -> list[ModelAttemptEvent]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    events: list[ModelAttemptEvent] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and not text.endswith("\n"):
                break
            raise ModelObservabilityError(f"invalid JSON at {path}:{index + 1}") from exc
        if not isinstance(value, dict):
            raise ModelObservabilityError(f"model event at {path}:{index + 1} is not an object")
        events.append(ModelAttemptEvent.from_dict(value))
    return events


def append_event(path: Path, event: ModelAttemptEvent) -> Literal["appended", "duplicate"]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    encoded = _canonical(event.to_dict())
    with cc.collab_lock(lock_path, ttl=10.0, acquire_timeout=30.0):
        needs_newline = _tail_needs_newline(path) if path.exists() else False
        index = _event_index(path) if path.exists() else {}
        existing = index.get(event.event_id)
        if existing is not None:
            if existing == encoded:
                return "duplicate"
            raise ModelObservabilityError(
                f"event_id {event.event_id!r} already exists with different content"
            )
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(("\n" if needs_newline else "") + encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        stat = path.stat()
        index[event.event_id] = encoded
        with _EVENT_INDEX_LOCK:
            _EVENT_INDEX_CACHE[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size, index)
    return "appended"


def append_with_health(
    path: Path, event: ModelAttemptEvent
) -> Literal["appended", "duplicate"]:
    """Append one event and make any persistence loss durable and operator-visible."""
    try:
        result = append_event(path, event)
    except Exception as exc:
        with suppress(Exception):
            _write_persistence_health(
                path,
                "unavailable",
                failed_event_id=event.event_id,
                failure_class=type(exc).__name__,
                run_uid=event.run_uid,
            )
        raise ModelObservabilityError(
            f"model-attempt persistence failed for event {event.event_id}"
        ) from exc
    with suppress(Exception):
        # The immutable event is already durable. A stale failure sidecar is safer than
        # claiming recovery when the health update itself could not persist.
        _write_persistence_health(path, "healthy", run_uid=event.run_uid)
    return result


def append_if_configured(event: ModelAttemptEvent) -> Literal["appended", "duplicate", "disabled"]:
    path = event_log_path()
    if path is None:
        return "disabled"
    return append_with_health(path, event)


def find_by_request(path: Path, request_id: str) -> ModelAttemptEvent | None:
    matches = [event for event in read_events(path) if event.request_id == request_id]
    return sorted(matches, key=lambda event: (event.event_ts, event.event_id))[0] if matches else None


def reduce_attempts(events: Iterable[ModelAttemptEvent]) -> list[dict[str, Any]]:
    unique: dict[str, ModelAttemptEvent] = {}
    collisions: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for event in events:
        prior = unique.get(event.event_id)
        if prior is not None:
            if prior != event:
                collisions.setdefault((event.run_uid, event.attempt_id), []).append(
                    {"kind": "event_id_collision", "event_id": event.event_id}
                )
            continue
        unique[event.event_id] = event

    groups: dict[tuple[str | None, str], list[ModelAttemptEvent]] = {}
    for event in unique.values():
        groups.setdefault((event.run_uid, event.attempt_id), []).append(event)

    attempts: list[dict[str, Any]] = []
    for key, items in groups.items():
        ordered = sorted(items, key=lambda event: event.event_ts)
        conflicts = list(collisions.get(key, []))
        terminal: ModelAttemptEvent | None = None
        telemetry: ModelAttemptEvent | None = None
        execution_state: str | None = None
        telemetry_state: str | None = None
        for event in ordered:
            if event.state in _TELEMETRY_STATES:
                telemetry_state = event.state
                telemetry = event
                continue
            if terminal is not None:
                conflicts.append(
                    {
                        "kind": "event_after_terminal",
                        "event_id": event.event_id,
                        "terminal_event_id": terminal.event_id,
                    }
                )
                continue
            execution_state = event.state
            if event.state in _EXECUTION_TERMINAL:
                terminal = event
        first = ordered[0]
        execution_events = [event for event in ordered if event.state not in _TELEMETRY_STATES]
        tool_activity = [
            {
                "event_ts": event.event_ts,
                "state": event.state,
                **dict(event.detail),
            }
            for event in ordered
            if event.source == "repo_seat_tool"
        ]
        latest = terminal or (execution_events[-1] if execution_events else ordered[-1])
        attempts.append(
            {
                "run_uid": first.run_uid,
                "attempt_id": first.attempt_id,
                "request_id": first.request_id,
                "seat": first.seat,
                "requested_model": first.requested_model,
                "source": first.source,
                "parent_attempt_id": first.parent_attempt_id,
                "handoff_id": first.handoff_id,
                "candidate_id": first.candidate_id,
                "state": execution_state,
                "states": [event.state for event in ordered],
                "telemetry_state": telemetry_state,
                "started_ts": first.event_ts,
                "completed_ts": terminal.event_ts if terminal is not None else None,
                "updated_ts": ordered[-1].event_ts,
                "actual_model": latest.actual_model,
                "provider": latest.provider,
                "gateway_route": latest.gateway_route,
                "gateway_request_id": latest.gateway_request_id,
                "provider_request_id": latest.provider_request_id,
                "completion_status": latest.completion_status,
                "failure_classification": latest.failure_classification,
                "first_token_latency_ms": latest.first_token_latency_ms,
                "total_duration_ms": latest.total_duration_ms,
                "streaming": latest.streaming,
                "tokens": latest.tokens,
                "cost": latest.cost,
                "retry_count": latest.retry_count,
                "last_activity": dict(latest.detail),
                "tool_activity": tool_activity,
                "telemetry_result": telemetry.telemetry_result if telemetry else None,
                "observation_id": telemetry.observation_id if telemetry else None,
                "trace_id": telemetry.trace_id if telemetry else None,
                "conflicts": conflicts,
            }
        )
    return sorted(attempts, key=lambda item: (str(item["started_ts"]), str(item["attempt_id"])))
