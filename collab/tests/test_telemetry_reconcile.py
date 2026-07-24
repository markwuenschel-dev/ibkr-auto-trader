"""Read-only Langfuse reconciliation for LiteLLM-owned generation telemetry."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import model_observability as mo  # noqa: E402
import telemetry_reconcile as tr  # noqa: E402


def _attempt(
    request_id: str, *, ended: str, retry: int = 0, model: str = "grok-4.5"
) -> list[mo.ModelAttemptEvent]:
    common = {
        "attempt_id": f"attempt-{request_id}",
        "request_id": request_id,
        "run_uid": "run-1",
        "seat": "reviewer",
        "requested_model": model,
        "attempt_number": retry + 1,
        "source": "gateway_client",
        "retry_count": retry,
    }
    return [
        mo.ModelAttemptEvent(
            event_id=f"start-{request_id}",
            state="connecting",
            event_ts="2026-07-22T00:00:00Z",
            **common,
        ),
        mo.ModelAttemptEvent(
            event_id=f"done-{request_id}", state="completed", event_ts=ended, **common
        ),
    ]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_is_bounded_paginated_and_excludes_prompt_output_fields() -> None:
    seen = []
    pages = [
        {
            "data": [
                {
                    "id": "obs-1",
                    "type": "GENERATION",
                    "traceId": "trace-1",
                    "metadata": {"requester_metadata": "{'request_id': 'request-1'}"},
                }
            ],
            "meta": {"cursor": "next"},
        },
        {"data": [], "meta": {"cursor": None}},
    ]

    def opener(request, timeout):
        seen.append(request.full_url)
        return Response(json.dumps(pages.pop(0)).encode())

    source = tr.LangfuseObservationSource(
        tr.LangfuseConfig("https://us.cloud.langfuse.com", "pk-test", "sk-test"), opener=opener
    )
    rows = source.fetch(
        datetime(2026, 7, 22, tzinfo=UTC), datetime(2026, 7, 22, 0, 5, tzinfo=UTC)
    )
    assert [row["id"] for row in rows] == ["obs-1"]
    assert len(seen) == 2 and "cursor=next" in seen[1]
    assert all("fromStartTime=" in url and "toStartTime=" in url for url in seen)
    fields = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["fields"][0] for url in seen]
    assert all("io" not in value.lower() and "input" not in value.lower() for value in fields)


def test_reconcile_verifies_matches_and_preserves_retry_identity(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    events = [
        *_attempt("request-1", ended="2026-07-22T00:00:02Z"),
        *_attempt("request-2", ended="2026-07-22T00:00:03Z", retry=1),
    ]
    for event in events:
        mo.append_event(path, event)
    observations = [
        {
            "id": "obs-1",
            "type": "GENERATION",
            "traceId": "trace-1",
            "metadata": {"requester_metadata": "{'request_id': 'request-1'}"},
        },
        {
            "id": "obs-2",
            "type": "GENERATION",
            "traceId": "trace-2",
            "metadata": {"requester_metadata": "{'request_id': 'request-2'}"},
        },
    ]
    summary = tr.reconcile_events(
        path,
        observations=observations,
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )
    assert summary == {
        "attempts": 2,
        "verified": 2,
        "missing": 0,
        "pending": 0,
        "ambiguous": 0,
    }
    reduced = mo.reduce_attempts(mo.read_events(path))
    assert [item["telemetry_state"] for item in reduced] == [
        "telemetry_verified",
        "telemetry_verified",
    ]
    assert {item["request_id"] for item in reduced} == {"request-1", "request-2"}

    # Deterministic verification event IDs make reconciliation idempotent.
    assert tr.reconcile_events(
        path,
        observations=observations,
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    ) == {
        "attempts": 2,
        "verified": 2,
        "missing": 0,
        "pending": 0,
        "ambiguous": 0,
    }


def test_each_supported_alias_reconciles_to_a_distinct_langfuse_generation(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    aliases = ("gpt-5.6-luna", "grok-4.5", "gemini-3.5-flash", "haiku-4.5")
    observations = []
    for index, alias in enumerate(aliases):
        request_id = f"request-{index}"
        for event in _attempt(
            request_id,
            ended=f"2026-07-22T00:00:0{index + 2}Z",
            model=alias,
        ):
            mo.append_event(path, event)
        observations.append(
            {
                "id": f"observation-{index}",
                "type": "GENERATION",
                "traceId": f"trace-{index}",
                "metadata": {"requester_metadata": {"request_id": request_id}},
            }
        )

    summary = tr.reconcile_events(
        path,
        observations=observations,
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )
    assert summary == {
        "attempts": 4,
        "verified": 4,
        "missing": 0,
        "pending": 0,
        "ambiguous": 0,
    }
    reduced = mo.reduce_attempts(mo.read_events(path))
    assert {item["requested_model"] for item in reduced} == set(aliases)
    assert {item["observation_id"] for item in reduced} == {
        "observation-0",
        "observation-1",
        "observation-2",
        "observation-3",
    }
    assert all(item["telemetry_state"] == "telemetry_verified" for item in reduced)


def test_missing_after_grace_is_explicit_but_recent_attempt_stays_pending(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    for event in _attempt("old", ended="2026-07-22T00:00:02Z"):
        mo.append_event(path, event)
    for event in _attempt("recent", ended="2026-07-22T00:09:50Z"):
        mo.append_event(path, event)

    summary = tr.reconcile_events(
        path,
        observations=[],
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )
    assert summary == {
        "attempts": 2,
        "verified": 0,
        "missing": 1,
        "pending": 1,
        "ambiguous": 0,
    }
    missing = [event for event in mo.read_events(path) if event.state == "telemetry_failed"]
    assert len(missing) == 1
    assert missing[0].request_id == "old"
    assert missing[0].telemetry_result == "missing"
    assert missing[0].detail == {"reason": "observation_missing_after_grace_period"}


def test_source_failure_is_recorded_as_verification_failure(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    for event in _attempt("request-1", ended="2026-07-22T00:00:02Z"):
        mo.append_event(path, event)

    def unavailable(request, timeout):
        raise urllib.error.URLError("offline with secret detail")

    source = tr.LangfuseObservationSource(
        tr.LangfuseConfig("https://us.cloud.langfuse.com", "pk-test", "sk-test"),
        opener=unavailable,
    )
    summary = tr.reconcile_source(
        path,
        source,
        started=datetime(2026, 7, 22, tzinfo=UTC),
        ended=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )
    assert summary["verification_failed"] == 1
    failure = next(event for event in mo.read_events(path) if event.state == "telemetry_failed")
    assert failure.telemetry_result == "verification_failed"
    assert failure.detail == {"reason": "langfuse_query_failed", "failure_class": "URLError"}
    assert "secret detail" not in path.read_text("utf-8")


def test_reconcile_ignores_non_generation_observation_with_matching_request_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model-events.jsonl"
    for event in _attempt("request-1", ended="2026-07-22T00:00:02Z"):
        mo.append_event(path, event)

    summary = tr.reconcile_events(
        path,
        observations=[
            {
                "id": "span-1",
                "type": "SPAN",
                "traceId": "trace-1",
                "metadata": {"requester_metadata": {"request_id": "request-1"}},
            }
        ],
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )

    assert summary == {
        "attempts": 1,
        "verified": 0,
        "missing": 1,
        "pending": 0,
        "ambiguous": 0,
    }


def test_reconcile_records_ambiguous_generation_matches_without_guessing(tmp_path: Path) -> None:
    path = tmp_path / "model-events.jsonl"
    for event in _attempt("request-1", ended="2026-07-22T00:00:02Z"):
        mo.append_event(path, event)
    observations = [
        {
            "id": observation_id,
            "type": "GENERATION",
            "traceId": "trace-1",
            "metadata": {"requester_metadata": {"request_id": "request-1"}},
        }
        for observation_id in ("generation-1", "generation-2")
    ]

    summary = tr.reconcile_events(
        path,
        observations=observations,
        now=datetime(2026, 7, 22, 0, 10, tzinfo=UTC),
        grace_seconds=60,
        run_uid="run-1",
    )

    assert summary == {
        "attempts": 1,
        "verified": 0,
        "missing": 0,
        "pending": 0,
        "ambiguous": 1,
    }
    failure = next(event for event in mo.read_events(path) if event.state == "telemetry_failed")
    assert failure.telemetry_result == "ambiguous"
    assert failure.detail == {
        "reason": "multiple_generation_observations",
        "match_count": 2,
    }
