"""Canonical operator-facing lifecycle state, append-only history, and replay.

The file-backed collab has several authoritative source records (board directories,
lease/status, escalation artifacts, operator requests, and close transitions).  This
module owns the *one* typed contract used to describe their operator meaning and the
append-only per-handoff history used to retain it.  Source reconciliation belongs here;
renderers must not invent their own state tables.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402

SCHEMA_VERSION = "1.0"
_ENTITY_RE = re.compile(r"^\d{1,9}$")


class OperationalStateError(cc.CollabError):
    """Malformed, incompatible, or unsafe operational-state data."""


class OperationalState(str, Enum):  # noqa: UP042 - the public contract explicitly requires str + Enum
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING = "awaiting"
    PAUSED = "paused"
    CAPPED = "capped"
    BLOCKED = "blocked"
    PARKED = "parked"
    ESCALATED = "escalated"
    RETRYING = "retrying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


# A reducer sometimes has several simultaneous source facts.  The effective state is
# selected by this explicit table; every lower-priority fact remains in conditions or
# conflicts, so precedence never erases contradictory evidence.
STATE_PRECEDENCE: dict[OperationalState, int] = {
    OperationalState.COMPLETED: 140,
    OperationalState.CANCELLED: 130,
    OperationalState.SUPERSEDED: 120,
    OperationalState.ESCALATED: 110,
    OperationalState.FAILED: 100,
    OperationalState.BLOCKED: 90,
    OperationalState.CAPPED: 80,
    OperationalState.PAUSED: 70,
    OperationalState.RETRYING: 60,
    OperationalState.RUNNING: 50,
    OperationalState.AWAITING: 45,
    OperationalState.PARKED: 40,
    OperationalState.CLAIMED: 30,
    OperationalState.QUEUED: 20,
}

# Legal producer transitions.  Reconciliation is the only source allowed to
# cross this matrix because it records an observed correction rather than a
# workflow command.  Same-state events are always legal: they retain meaningful
# sub-phase changes (for example running/verify -> running/assess) without
# inventing extra top-level states.
ALLOWED_TRANSITIONS: dict[OperationalState, frozenset[OperationalState]] = {
    OperationalState.QUEUED: frozenset(
        {
            OperationalState.CLAIMED,
            OperationalState.BLOCKED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
        }
    ),
    OperationalState.CLAIMED: frozenset(
        {
            OperationalState.RUNNING,
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.RUNNING: frozenset(
        {
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.AWAITING: frozenset(
        {
            OperationalState.CLAIMED,
            OperationalState.RUNNING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.PAUSED: frozenset(
        {
            OperationalState.RUNNING,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.CAPPED: frozenset(
        {
            OperationalState.PAUSED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.BLOCKED: frozenset(
        {
            OperationalState.RUNNING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.PARKED: frozenset(
        {
            OperationalState.RUNNING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.ESCALATED: frozenset(
        {
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.RETRYING: frozenset(
        {
            OperationalState.CLAIMED,
            OperationalState.RUNNING,
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
            OperationalState.COMPLETED,
        }
    ),
    OperationalState.FAILED: frozenset(
        {
            OperationalState.RETRYING,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
        }
    ),
    OperationalState.CANCELLED: frozenset(),
    OperationalState.SUPERSEDED: frozenset(),
    OperationalState.COMPLETED: frozenset(),
}


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state(value: Any, *, field: str) -> OperationalState | None:
    if value is None:
        return None
    try:
        return OperationalState(value)
    except (TypeError, ValueError) as exc:
        raise OperationalStateError(f"invalid {field}: {value!r}") from exc


def _required_state(doc: dict[str, Any], key: str) -> OperationalState:
    value = _state(doc.get(key), field=key)
    if value is None:
        raise OperationalStateError(f"operational event {key} is required")
    return value


def _required_string(doc: dict[str, Any], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationalStateError(f"operational event {key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class OperationalEvent:
    event_id: str
    entity_id: str
    previous_state: OperationalState | None
    new_state: OperationalState
    reason: str
    source: str
    actor: str | None
    run_id: str | None
    event_ts: str
    ingested_ts: str
    correlation_id: str | None
    trace_id: str | None
    conditions: tuple[str, ...] = ()
    escalation_severity: str | None = None
    escalation_reason: str | None = None
    escalation_ts: str | None = None
    required_action: str | None = None
    sequence: int | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "previous_state": self.previous_state.value if self.previous_state else None,
            "new_state": self.new_state.value,
            "reason": self.reason,
            "source": self.source,
            "actor": self.actor,
            "run_id": self.run_id,
            "event_ts": self.event_ts,
            "ingested_ts": self.ingested_ts,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "conditions": list(self.conditions),
            "escalation_severity": self.escalation_severity,
            "escalation_reason": self.escalation_reason,
            "escalation_ts": self.escalation_ts,
            "required_action": self.required_action,
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> OperationalEvent:
        if not isinstance(doc, dict):
            raise OperationalStateError("operational event must be an object")
        version = doc.get("schema_version")
        if version != SCHEMA_VERSION:
            raise OperationalStateError(
                f"incompatible operational event schema {version!r}; expected {SCHEMA_VERSION!r}"
            )
        sequence = doc.get("sequence")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
        ):
            raise OperationalStateError("operational event sequence must be a positive integer")
        raw_conditions = doc.get("conditions") or []
        if not isinstance(raw_conditions, list) or not all(isinstance(v, str) for v in raw_conditions):
            raise OperationalStateError("operational event conditions must be a list of strings")
        return cls(
            schema_version=SCHEMA_VERSION,
            sequence=sequence,
            event_id=_required_string(doc, "event_id"),
            entity_id=_validate_entity_id(_required_string(doc, "entity_id")),
            previous_state=_state(doc.get("previous_state"), field="previous_state"),
            new_state=_required_state(doc, "new_state"),
            reason=_required_string(doc, "reason"),
            source=_required_string(doc, "source"),
            actor=doc.get("actor") if isinstance(doc.get("actor"), str) else None,
            run_id=doc.get("run_id") if isinstance(doc.get("run_id"), str) else None,
            event_ts=_required_string(doc, "event_ts"),
            ingested_ts=_required_string(doc, "ingested_ts"),
            correlation_id=(
                doc.get("correlation_id") if isinstance(doc.get("correlation_id"), str) else None
            ),
            trace_id=doc.get("trace_id") if isinstance(doc.get("trace_id"), str) else None,
            conditions=tuple(raw_conditions),
            escalation_severity=(
                doc.get("escalation_severity") if isinstance(doc.get("escalation_severity"), str) else None
            ),
            escalation_reason=(
                doc.get("escalation_reason") if isinstance(doc.get("escalation_reason"), str) else None
            ),
            escalation_ts=doc.get("escalation_ts") if isinstance(doc.get("escalation_ts"), str) else None,
            required_action=(
                doc.get("required_action") if isinstance(doc.get("required_action"), str) else None
            ),
        )


@dataclass(frozen=True)
class StateConflict:
    kind: str
    event_id: str
    message: str


@dataclass(frozen=True)
class Reduction:
    state: OperationalState | None
    events: tuple[OperationalEvent, ...]
    conflicts: tuple[StateConflict, ...]
    duplicate_count: int


@dataclass(frozen=True)
class AppendResult:
    event: OperationalEvent
    appended: bool


@dataclass(frozen=True)
class HistoryPage:
    events: tuple[OperationalEvent, ...]
    next_cursor: int | None
    rejected_count: int
    schema_incompatible: bool


@dataclass(frozen=True)
class OperationalItem:
    id: str
    slug: str
    board_state: str
    effective_state: OperationalState
    state_reason: str
    state_source: str
    state_ts: str | None
    owner: str | None
    actor: str | None
    run_id: str | None
    correlation_id: str | None
    trace_id: str | None
    conditions: tuple[str, ...]
    escalation: dict[str, Any] | None
    required_action: str | None
    conflicts: tuple[dict[str, Any], ...]
    freshness: dict[str, Any]
    source_evidence: dict[str, Any]
    history_count: int
    to: str | None
    from_: str | None
    age_s: float | None
    closed_by: str | None
    closed_label: str | None
    closed_actor: str | None
    closed_reason: str | None
    closed_autonomously: bool
    source_read_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "slug": self.slug,
            # Compatibility: the directory/CAS state remains the action-routing state.
            "state": self.board_state,
            "board_state": self.board_state,
            "operational_state": self.effective_state.value,
            "state_reason": self.state_reason,
            "state_source": self.state_source,
            "state_ts": self.state_ts,
            "owner": self.owner,
            "actor": self.actor,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "conditions": list(self.conditions),
            "escalated": self.escalation is not None,
            "escalation_reason": (self.escalation or {}).get("reason"),
            "escalation": self.escalation,
            "required_action": self.required_action,
            "conflicts": list(self.conflicts),
            "freshness": self.freshness,
            "source_evidence": self.source_evidence,
            "history_count": self.history_count,
            "to": self.to,
            "from": self.from_,
            "age_s": self.age_s,
            "closed_by": self.closed_by,
            "closed_label": self.closed_label,
            "closed_actor": self.closed_actor,
            "closed_reason": self.closed_reason,
            "closed_autonomously": self.closed_autonomously,
            "source_read_status": self.source_read_status,
        }


def _history_health_path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "lifecycle" / "health.json"


def _record_history_health(collab: str | Path, *, status: str, reason: str | None) -> None:
    path = _history_health_path(collab)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cc.safe_write(
            path,
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": status,
                    "reason": reason,
                    "updated_ts": _now_utc(),
                },
                separators=(",", ":"),
            )
            + "\n",
        )
    except Exception:
        return


def read_history_health(collab: str | Path) -> dict[str, Any]:
    try:
        doc = json.loads(_history_health_path(collab).read_text("utf-8"))
    except OSError, ValueError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unknown",
            "reason": "no history persistence result has been recorded",
            "updated_ts": None,
        }
    return doc if isinstance(doc, dict) else {"status": "unavailable", "reason": "malformed health record"}


def _validate_entity_id(entity_id: str) -> str:
    if not isinstance(entity_id, str) or not _ENTITY_RE.fullmatch(entity_id):
        raise OperationalStateError(f"invalid operational entity id: {entity_id!r}")
    return entity_id


def _history_path(collab: str | Path, entity_id: str) -> Path:
    return Path(collab) / "autopilot" / "lifecycle" / f"{_validate_entity_id(entity_id)}.jsonl"


def _same_payload(left: OperationalEvent, right: OperationalEvent) -> bool:
    return replace(left, sequence=None) == replace(right, sequence=None)


def append_event(collab: str | Path, event: OperationalEvent) -> AppendResult:
    """Append exactly once by stable event ID and assign a per-entity sequence."""
    if event.schema_version != SCHEMA_VERSION:
        raise OperationalStateError(f"cannot append schema {event.schema_version!r}")
    _validate_entity_id(event.entity_id)
    path = _history_path(collab, event.entity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lockdir = path.with_name(path.name + ".lock")
    with cc.collab_lock(lockdir, ttl=10.0, acquire_timeout=30.0) as holder:
        holder.assert_current()
        page = read_history(collab, event.entity_id)
        for existing in page.events:
            if existing.event_id != event.event_id:
                continue
            if not _same_payload(existing, event):
                raise OperationalStateError(
                    f"event_id {event.event_id!r} already exists with different content"
                )
            return AppendResult(existing, False)
        next_sequence = max((item.sequence or 0 for item in page.events), default=0) + 1
        stamped = replace(
            event,
            sequence=next_sequence,
            ingested_ts=event.ingested_ts or _now_utc(),
        )
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(stamped.to_dict(), separators=(",", ":")) + "\n")
        _record_history_health(collab, status="healthy", reason=None)
        return AppendResult(stamped, True)


def _all_history(collab: str | Path, entity_id: str) -> tuple[OperationalEvent, ...]:
    out: list[OperationalEvent] = []
    cursor: int | None = None
    while True:
        page = read_history(collab, entity_id, after=cursor, limit=1000)
        out.extend(page.events)
        if not page.events or page.next_cursor == cursor or len(page.events) < 1000:
            break
        cursor = page.next_cursor
    return tuple(out)


def record_transition(
    collab: str | Path,
    entity_id: str,
    new_state: OperationalState | str,
    *,
    reason: str,
    source: str,
    actor: str | None = None,
    run_id: str | None = None,
    event_ts: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    conditions: tuple[str, ...] = (),
    escalation_severity: str | None = None,
    escalation_reason: str | None = None,
    escalation_ts: str | None = None,
    required_action: str | None = None,
) -> AppendResult | None:
    """Record a committed source transition without making its primary write unsafe."""
    _validate_entity_id(entity_id)
    try:
        state = new_state if isinstance(new_state, OperationalState) else OperationalState(new_state)
    except ValueError as exc:
        raise OperationalStateError(f"invalid new_state: {new_state!r}") from exc
    if not reason.strip() or not source.strip():
        raise OperationalStateError("transition reason and source are required")
    try:
        history = _all_history(collab, entity_id)
        reduced = reduce_history(history)
        latest = reduced.events[-1] if reduced.events else None
        if (
            latest is not None
            and latest.new_state is state
            and latest.reason == reason
            and latest.source == source
            and latest.run_id == run_id
            and latest.conditions == conditions
        ):
            return AppendResult(latest, False)
        now = event_ts or _now_utc()
        event = OperationalEvent(
            event_id=str(uuid.uuid4()),
            entity_id=entity_id,
            previous_state=reduced.state,
            new_state=state,
            reason=reason,
            source=source,
            actor=actor,
            run_id=run_id,
            event_ts=now,
            ingested_ts=_now_utc(),
            correlation_id=correlation_id,
            trace_id=trace_id,
            conditions=conditions,
            escalation_severity=escalation_severity,
            escalation_reason=escalation_reason,
            escalation_ts=escalation_ts,
            required_action=required_action,
        )
        return append_event(collab, event)
    except Exception as exc:
        _record_history_health(collab, status="unavailable", reason=f"{type(exc).__name__}: {exc}"[:300])
        return None


def read_history(
    collab: str | Path,
    entity_id: str,
    *,
    after: int | None = None,
    limit: int = 100,
) -> HistoryPage:
    """Read a stable page; malformed/future records are counted, never rounded healthy."""
    _validate_entity_id(entity_id)
    if after is not None and (isinstance(after, bool) or not isinstance(after, int) or after < 0):
        raise OperationalStateError("history cursor must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 1000):
        raise OperationalStateError("history limit must be in 1..1000")
    try:
        lines = _history_path(collab, entity_id).read_text("utf-8").splitlines()
    except OSError:
        return HistoryPage((), after, 0, False)
    events: list[OperationalEvent] = []
    rejected = 0
    incompatible = False
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if isinstance(raw, dict) and raw.get("schema_version") != SCHEMA_VERSION:
                incompatible = True
            parsed = OperationalEvent.from_dict(raw)
        except (ValueError, OperationalStateError):
            rejected += 1
            continue
        if parsed.entity_id != entity_id:
            rejected += 1
            continue
        if (parsed.sequence or 0) > (after or 0):
            events.append(parsed)
    events.sort(key=lambda item: (item.sequence or 0, item.ingested_ts, item.event_id))
    selected = tuple(events[:limit])
    cursor = selected[-1].sequence if selected else after
    return HistoryPage(selected, cursor, rejected, incompatible)


def _sort_key(event: OperationalEvent) -> tuple[str, str, int, str]:
    return (event.event_ts, event.ingested_ts, event.sequence or 0, event.event_id)


def reduce_history(events: Iterable[OperationalEvent | dict[str, Any]]) -> Reduction:
    """Deterministically replay records while retaining contradictions as conflicts."""
    unique: dict[str, OperationalEvent] = {}
    duplicates = 0
    conflicts: list[StateConflict] = []
    for raw in events:
        item = raw if isinstance(raw, OperationalEvent) else OperationalEvent.from_dict(raw)
        existing = unique.get(item.event_id)
        if existing is None:
            unique[item.event_id] = item
        elif _same_payload(existing, item):
            duplicates += 1
        else:
            conflicts.append(
                StateConflict(
                    "event_id_collision",
                    item.event_id,
                    "the same event_id carried different immutable content",
                )
            )

    ordered = tuple(sorted(unique.values(), key=_sort_key))
    current: OperationalState | None = None
    for item in ordered:
        if item.previous_state is not None and item.previous_state is not current:
            conflicts.append(
                StateConflict(
                    "previous_state_mismatch",
                    item.event_id,
                    (
                        f"event expected {item.previous_state.value!r}, replay was "
                        f"{getattr(current, 'value', None)!r}"
                    ),
                )
            )
        if (
            current is not None
            and item.new_state is not current
            and item.source != "reconciliation"
            and item.new_state not in ALLOWED_TRANSITIONS[current]
        ):
            conflicts.append(
                StateConflict(
                    "invalid_transition",
                    item.event_id,
                    f"transition {current.value!r} -> {item.new_state.value!r} is not permitted",
                )
            )
        current = item.new_state
    return Reduction(current, ordered, tuple(conflicts), duplicates)


_UNSET = object()
_BOARD_STATE = {
    "pending": OperationalState.QUEUED,
    "claimed": OperationalState.CLAIMED,
    "done": OperationalState.COMPLETED,
    "archive": OperationalState.COMPLETED,
}
_BOARD_COMPATIBLE = {
    "pending": frozenset(
        {
            OperationalState.QUEUED,
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
        }
    ),
    "claimed": frozenset(
        {
            OperationalState.CLAIMED,
            OperationalState.RUNNING,
            OperationalState.AWAITING,
            OperationalState.PAUSED,
            OperationalState.CAPPED,
            OperationalState.BLOCKED,
            OperationalState.PARKED,
            OperationalState.ESCALATED,
            OperationalState.RETRYING,
            OperationalState.FAILED,
            OperationalState.CANCELLED,
            OperationalState.SUPERSEDED,
        }
    ),
    "done": frozenset({OperationalState.COMPLETED}),
    "archive": frozenset({OperationalState.COMPLETED}),
}


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_ts(value)
    return round(max(0.0, (now - parsed).total_seconds()), 1) if parsed else None


def _source_fact(
    state: OperationalState,
    *,
    reason: str,
    source: str,
    ts: str | None = None,
    owner: str | None = None,
    actor: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    conditions: tuple[str, ...] = (),
    required_action: str | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "source": source,
        "ts": ts,
        "owner": owner,
        "actor": actor,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "conditions": conditions,
        "required_action": required_action,
    }


def reconcile_item(
    collab: str | Path,
    handoff: dict[str, Any],
    *,
    status: dict[str, Any] | None = None,
    live: bool = False,
    escalation_record: dict[str, Any] | None | object = _UNSET,
    request: dict[str, Any] | None | object = _UNSET,
    transition_record: dict[str, Any] | None | object = _UNSET,
    now: datetime | None = None,
) -> OperationalItem:
    """Reconcile retained source facts into one operator meaning without erasing disagreement."""
    import escalation as _escalation
    import operator_requests as _requests
    import transitions as _transitions

    now = now or datetime.now(UTC)
    hid = _validate_entity_id(str(handoff.get("id") or ""))
    board_state = str(handoff.get("state") or "")
    if board_state not in _BOARD_STATE:
        raise OperationalStateError(f"unknown board state for {hid}: {board_state!r}")
    escalation_record = (
        _escalation.read(collab, hid) if escalation_record is _UNSET else escalation_record
    )
    request = _requests.get(collab, hid) if request is _UNSET else request
    transition_record = _transitions.read(collab, hid) if transition_record is _UNSET else transition_record

    try:
        stat = Path(str(handoff.get("path"))).stat()
        age_s = round(max(0.0, now.timestamp() - stat.st_mtime), 1)
        board_ts = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    except (OSError, ValueError):
        age_s = None
        board_ts = None

    facts: list[dict[str, Any]] = []
    tr = transition_record if isinstance(transition_record, dict) else None
    if board_state in ("done", "archive"):
        facts.append(
            _source_fact(
                OperationalState.COMPLETED,
                reason=str((tr or {}).get("reason") or (tr or {}).get("kind") or board_state),
                source="transition_record" if tr else "handoff_directory",
                ts=(tr or {}).get("ts") or board_ts,
                owner=handoff.get("to"),
                actor=(tr or {}).get("actor"),
                correlation_id=(tr or {}).get("candidate_id"),
                conditions=("archived",) if board_state == "archive" else (),
            )
        )

    esc = escalation_record if isinstance(escalation_record, dict) else None
    if esc is not None:
        facts.append(
            _source_fact(
                OperationalState.ESCALATED,
                reason=str(esc.get("reason") or "escalated"),
                source="escalation_store",
                ts=esc.get("timestamp"),
                owner=handoff.get("to"),
                actor="autopilot",
                run_id=esc.get("run_uid"),
                conditions=("parked",),
                required_action=esc.get("required_action") or "retry_or_adopt",
            )
        )

    req = request if isinstance(request, dict) else None
    if req is not None:
        action = req.get("action")
        facts.append(
            _source_fact(
                OperationalState.RETRYING if action == "retry" else OperationalState.AWAITING,
                reason=f"operator_{action}_requested",
                source="operator_request",
                ts=req.get("requested_ts"),
                owner=handoff.get("to"),
                actor=req.get("requested_by"),
                conditions=(f"request:{action}",),
                required_action="start_driver",
            )
        )

    status_fact: dict[str, Any] | None = None
    if live and isinstance(status, dict) and str(status.get("current_hid") or "") == hid:
        phase = status.get("phase")
        mapped = {
            "thinking": OperationalState.RUNNING,
            "paused": OperationalState.PAUSED,
            "capped": OperationalState.CAPPED,
        }.get(phase)
        if mapped is not None:
            detail = status.get("stage") if phase == "thinking" else phase
            status_fact = _source_fact(
                mapped,
                reason=f"driver_{detail or phase}",
                source="autopilot_status",
                ts=status.get("updated_ts"),
                owner=status.get("active_seat") or handoff.get("to"),
                actor="autopilot",
                run_id=status.get("run_uid"),
                conditions=(f"seat:{status.get('active_seat')}",) if status.get("active_seat") else (),
            )
            facts.append(status_fact)

    page = read_history(collab, hid, limit=1000)
    reduced = reduce_history(page.events)
    latest = reduced.events[-1] if reduced.events else None
    if latest is not None:
        facts.append(
            _source_fact(
                latest.new_state,
                reason=latest.reason,
                source=latest.source,
                ts=latest.event_ts,
                owner=handoff.get("to"),
                actor=latest.actor,
                run_id=latest.run_id,
                correlation_id=latest.correlation_id,
                trace_id=latest.trace_id,
                conditions=latest.conditions,
                required_action=latest.required_action,
            )
        )

    facts.append(
        _source_fact(
            _BOARD_STATE[board_state],
            reason=f"board_{board_state}",
            source="handoff_directory",
            ts=board_ts,
            owner=handoff.get("to"),
            conditions=("archived",) if board_state == "archive" else (),
        )
    )

    selected = max(facts, key=lambda fact: STATE_PRECEDENCE[fact["state"]])
    if latest is None:
        bootstrap = OperationalEvent(
            event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"collab-lifecycle:{Path(collab).resolve()}:{hid}")),
            entity_id=hid,
            previous_state=None,
            new_state=selected["state"],
            reason="legacy_source_reconstruction",
            source="reconciliation",
            actor="dashboard",
            run_id=selected.get("run_id"),
            event_ts=selected.get("ts") or _now_utc(),
            ingested_ts=_now_utc(),
            correlation_id=selected.get("correlation_id"),
            trace_id=selected.get("trace_id"),
            conditions=tuple(selected.get("conditions") or ()),
            escalation_severity=(esc or {}).get("severity"),
            escalation_reason=(esc or {}).get("reason"),
            escalation_ts=(esc or {}).get("timestamp"),
            required_action=selected.get("required_action"),
        )
        try:
            append_event(collab, bootstrap)
            page = read_history(collab, hid, limit=1000)
            reduced = reduce_history(page.events)
        except Exception as exc:
            _record_history_health(
                collab, status="unavailable", reason=f"{type(exc).__name__}: {exc}"[:300]
            )

    conflicts = [
        {"kind": conflict.kind, "event_id": conflict.event_id, "message": conflict.message}
        for conflict in reduced.conflicts
    ]
    if page.rejected_count:
        conflicts.append(
            {
                "kind": "rejected_history_records",
                "event_id": hid,
                "message": f"{page.rejected_count} lifecycle history record(s) were rejected",
            }
        )
    if page.schema_incompatible:
        conflicts.append(
            {
                "kind": "schema_incompatible",
                "event_id": hid,
                "message": "future or incompatible lifecycle history records are present",
            }
        )
    if selected["state"] not in _BOARD_COMPATIBLE[board_state]:
        conflicts.append(
            {
                "kind": "source_state_conflict",
                "event_id": hid,
                "message": (
                    f"board state {board_state!r} is incompatible with effective state "
                    f"{selected['state'].value!r}"
                ),
            }
        )
    if status_fact is not None and selected["state"] is not status_fact["state"]:
        conflicts.append(
            {
                "kind": "live_source_disagreement",
                "event_id": hid,
                "message": (
                    f"live status reports {status_fact['state'].value!r} while "
                    f"{selected['source']} reports {selected['state'].value!r}"
                ),
            }
        )

    conditions = set(selected.get("conditions") or ())
    if esc is not None:
        conditions.add("parked")
    if req is not None:
        conditions.add(f"request:{req.get('action')}")
    status_age = _age_seconds((status or {}).get("updated_ts"), now) if status_fact else None
    status_freshness = (
        "fresh"
        if status_age is not None and status_age <= 15.0
        else "stale"
        if status_age is not None
        else "unknown"
    )
    escalation_public = None
    if esc is not None:
        escalation_public = {
            key: esc.get(key)
            for key in (
                "hid",
                "reason",
                "severity",
                "timestamp",
                "run_uid",
                "required_action",
                "metadata_status",
            )
        }
    request_public = (
        {key: req.get(key) for key in ("hid", "action", "requested_by", "requested_ts", "note")}
        if req is not None
        else None
    )
    transition_public = (
        {key: tr.get(key) for key in ("kind", "actor", "reason", "ts", "candidate_id", "to")}
        if tr is not None
        else None
    )
    source_status = "healthy"
    if (esc or {}).get("metadata_status") in ("malformed", "legacy") or page.rejected_count:
        source_status = "degraded"
    if page.schema_incompatible:
        source_status = "unavailable"
    return OperationalItem(
        id=hid,
        slug=str(handoff.get("slug") or ""),
        board_state=board_state,
        effective_state=selected["state"],
        state_reason=str(selected["reason"]),
        state_source=str(selected["source"]),
        state_ts=selected.get("ts"),
        owner=selected.get("owner") or handoff.get("to"),
        actor=selected.get("actor"),
        run_id=selected.get("run_id"),
        correlation_id=selected.get("correlation_id"),
        trace_id=selected.get("trace_id"),
        conditions=tuple(sorted(conditions)),
        escalation=escalation_public,
        required_action=selected.get("required_action") or ((esc or {}).get("required_action")),
        conflicts=tuple(conflicts),
        freshness={
            "effective_age_s": _age_seconds(selected.get("ts"), now),
            "handoff_age_s": age_s,
            "live_status": status_freshness,
            "live_status_age_s": status_age,
        },
        source_evidence={
            "board_state": board_state,
            "board_ts": board_ts,
            "live_status": (
                {
                    key: status.get(key)
                    for key in ("phase", "stage", "active_seat", "current_hid", "run_uid", "updated_ts")
                }
                if status_fact and status
                else None
            ),
            "escalation": escalation_public,
            "operator_request": request_public,
            "transition": transition_public,
            "history_state": reduced.state.value if reduced.state else None,
        },
        history_count=len(reduced.events),
        to=handoff.get("to"),
        from_=handoff.get("from"),
        age_s=age_s,
        closed_by=(tr or {}).get("kind"),
        closed_label=_transitions.label_of(tr) if board_state in ("done", "archive") else None,
        closed_actor=(tr or {}).get("actor"),
        closed_reason=(tr or {}).get("reason"),
        closed_autonomously=_transitions.is_autonomous(tr),
        source_read_status=source_status,
    )
