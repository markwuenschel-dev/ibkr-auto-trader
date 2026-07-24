"""Read-only Langfuse reconciliation for LiteLLM-owned generation telemetry."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collab_common as cc
import model_observability as mo

_FIELDS = "core,basic,time,metadata,model,usage,trace_context"
_TERMINAL = {"completed", "failed", "timed_out", "cancelled"}


class TelemetryReconcileError(cc.CollabError):
    """Langfuse verification data is unavailable or malformed."""


@dataclass(frozen=True)
class LangfuseConfig:
    host: str
    public_key: str
    secret_key: str

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.host)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise TelemetryReconcileError("LANGFUSE_HOST must be an absolute http(s) URL")
        if not self.public_key or not self.secret_key:
            raise TelemetryReconcileError("Langfuse public and secret keys are required")

    @classmethod
    def from_env(cls) -> LangfuseConfig:
        return cls(
            (os.environ.get("LANGFUSE_HOST") or "").strip().rstrip("/"),
            (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip(),
            (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip(),
        )


class LangfuseObservationSource:
    """Bounded Observations API v2 reader that deliberately excludes model I/O fields."""

    def __init__(self, config: LangfuseConfig, *, opener=urllib.request.urlopen, timeout: float = 15.0):
        self.config = config
        self.opener = opener
        self.timeout = timeout

    def fetch(self, started: datetime, ended: datetime) -> list[dict[str, Any]]:
        if started.tzinfo is None or ended.tzinfo is None or ended < started:
            raise TelemetryReconcileError("a valid timezone-aware observation window is required")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        rows: list[dict[str, Any]] = []
        for _page in range(100):
            query = {
                "fromStartTime": _timestamp(started),
                "toStartTime": _timestamp(ended),
                "fields": _FIELDS,
                "limit": "100",
            }
            if cursor:
                query["cursor"] = cursor
            url = (
                self.config.host
                + "/api/public/v2/observations?"
                + urllib.parse.urlencode(query)
            )
            token = base64.b64encode(
                f"{self.config.public_key}:{self.config.secret_key}".encode("ascii")
            ).decode("ascii")
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            )
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise TelemetryReconcileError("Langfuse observations response is not a data object")
            rows.extend(row for row in payload["data"] if isinstance(row, dict))
            raw_meta = payload.get("meta")
            meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            raw_cursor = meta.get("cursor")
            cursor = str(raw_cursor) if raw_cursor else None
            if not cursor:
                return rows
            if cursor in seen_cursors:
                raise TelemetryReconcileError("Langfuse observations cursor repeated")
            seen_cursors.add(cursor)
        raise TelemetryReconcileError("Langfuse observations exceeded 100 pages")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _requester_metadata(observation: dict[str, Any]) -> Any:
    metadata = observation.get("metadata")
    return metadata.get("requester_metadata") if isinstance(metadata, dict) else None


def _matches_request(observation: dict[str, Any], request_id: str) -> bool:
    requester = _requester_metadata(observation)
    if isinstance(requester, dict):
        return requester.get("request_id") == request_id
    if not isinstance(requester, str):
        return False
    pattern = rf"['\"]?request_id['\"]?\s*[:=]\s*['\"]{re.escape(request_id)}['\"]"
    return re.search(pattern, requester) is not None


def _attempts(events: list[mo.ModelAttemptEvent], run_uid: str | None) -> list[list[mo.ModelAttemptEvent]]:
    groups: dict[tuple[str | None, str], list[mo.ModelAttemptEvent]] = defaultdict(list)
    for event in events:
        if event.source not in ("gateway_client", "langfuse_reconciler"):
            continue
        if run_uid is not None and event.run_uid != run_uid:
            continue
        groups[(event.run_uid, event.attempt_id)].append(event)
    return [sorted(group, key=lambda item: (item.event_ts, item.event_id)) for group in groups.values()]


def _terminal(group: list[mo.ModelAttemptEvent]) -> mo.ModelAttemptEvent | None:
    execution = [event for event in group if event.source == "gateway_client"]
    return next((event for event in reversed(execution) if event.state in _TERMINAL), None)


def _has_result(
    group: list[mo.ModelAttemptEvent], state: str, *, observation_id: str | None = None
) -> bool:
    return any(
        event.state == state and (observation_id is None or event.observation_id == observation_id)
        for event in group
    )


def _verification_event(
    anchor: mo.ModelAttemptEvent,
    *,
    state: mo.LifecycleState,
    event_ts: str,
    result: str,
    observation_id: str | None = None,
    trace_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> mo.ModelAttemptEvent:
    identity = f"{anchor.request_id}:{state}:{observation_id or result}"
    return mo.ModelAttemptEvent(
        event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"collab-langfuse:{identity}")),
        attempt_id=anchor.attempt_id,
        request_id=anchor.request_id,
        run_uid=anchor.run_uid,
        seat=anchor.seat,
        requested_model=anchor.requested_model,
        state=state,
        event_ts=event_ts,
        attempt_number=anchor.attempt_number,
        source="langfuse_reconciler",
        parent_attempt_id=anchor.parent_attempt_id,
        handoff_id=anchor.handoff_id,
        candidate_id=anchor.candidate_id,
        escalation_id=anchor.escalation_id,
        retry_count=anchor.retry_count,
        telemetry_result=result,
        observation_id=observation_id,
        trace_id=trace_id,
        detail=detail or {},
    )


def reconcile_events(
    path: Path,
    *,
    observations: list[dict[str, Any]],
    now: datetime,
    grace_seconds: float,
    run_uid: str | None = None,
) -> dict[str, int]:
    if now.tzinfo is None or grace_seconds < 0:
        raise TelemetryReconcileError("timezone-aware now and non-negative grace_seconds are required")
    events = mo.read_events(path)
    groups = _attempts(events, run_uid)
    summary = {
        "attempts": 0,
        "verified": 0,
        "missing": 0,
        "pending": 0,
        "ambiguous": 0,
    }
    for group in groups:
        anchor = next((event for event in group if event.source == "gateway_client"), None)
        if anchor is None:
            continue
        summary["attempts"] += 1
        terminal = _terminal(group)
        if terminal is None:
            summary["pending"] += 1
            continue
        matches_by_id = {
            str(row.get("id")): row
            for row in observations
            if str(row.get("type") or "").upper() == "GENERATION"
            and row.get("id")
            and _matches_request(row, anchor.request_id)
        }
        matches = list(matches_by_id.values())
        if len(matches) > 1:
            if not _has_result(group, "telemetry_failed"):
                mo.append_with_health(
                    path,
                    _verification_event(
                        anchor,
                        state="telemetry_failed",
                        event_ts=_timestamp(now),
                        result="ambiguous",
                        detail={
                            "reason": "multiple_generation_observations",
                            "match_count": len(matches),
                        },
                    ),
                )
            summary["ambiguous"] += 1
            continue
        if matches:
            observation = matches[0]
            observation_id = str(observation.get("id") or "")
            trace_id = str(observation.get("traceId") or "") or None
            if observation_id and not _has_result(
                group, "telemetry_verified", observation_id=observation_id
            ):
                mo.append_with_health(
                    path,
                    _verification_event(
                        anchor,
                        state="telemetry_verified",
                        event_ts=_timestamp(now),
                        result="verified",
                        observation_id=observation_id,
                        trace_id=trace_id,
                    ),
                )
            summary["verified"] += 1
            continue
        age = (now.astimezone(UTC) - _parse_timestamp(terminal.event_ts)).total_seconds()
        if age < grace_seconds:
            summary["pending"] += 1
            continue
        if not _has_result(group, "telemetry_failed"):
            mo.append_with_health(
                path,
                _verification_event(
                    anchor,
                    state="telemetry_failed",
                    event_ts=_timestamp(now),
                    result="missing",
                    detail={"reason": "observation_missing_after_grace_period"},
                ),
            )
        summary["missing"] += 1
    return summary


def _record_query_failure(
    path: Path, *, run_uid: str | None, now: datetime, failure_class: str
) -> int:
    events = mo.read_events(path)
    count = 0
    for group in _attempts(events, run_uid):
        anchor = next((event for event in group if event.source == "gateway_client"), None)
        if anchor is None or _terminal(group) is None:
            continue
        result = "verification_failed"
        event = _verification_event(
            anchor,
            state="telemetry_failed",
            event_ts=_timestamp(now),
            result=result,
            detail={"reason": "langfuse_query_failed", "failure_class": failure_class},
        )
        if not _has_result(group, "telemetry_failed"):
            mo.append_with_health(path, event)
        count += 1
    return count


def reconcile_source(
    path: Path,
    source: LangfuseObservationSource,
    *,
    started: datetime,
    ended: datetime,
    now: datetime,
    grace_seconds: float,
    run_uid: str | None = None,
) -> dict[str, int]:
    try:
        observations = source.fetch(started, ended)
    except Exception as exc:
        return {
            "attempts": len(_attempts(mo.read_events(path), run_uid)),
            "verified": 0,
            "missing": 0,
            "pending": 0,
            "ambiguous": 0,
            "verification_failed": _record_query_failure(
                path, run_uid=run_uid, now=now, failure_class=type(exc).__name__
            ),
        }
    return reconcile_events(
        path,
        observations=observations,
        now=now,
        grace_seconds=grace_seconds,
        run_uid=run_uid,
    )
