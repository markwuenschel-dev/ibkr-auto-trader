"""Fail-closed LiteLLM transport contract and redacted per-attempt telemetry."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collab_common as cc
import model_observability as mo

_PROVIDER_HOST_SUFFIXES = (
    "api.openai.com",
    "api.x.ai",
    "anthropic.com",
    "googleapis.com",
    "generativelanguage.googleapis.com",
)


class GatewayConfigError(cc.CollabError):
    """Gateway configuration is absent or could bypass the controlled proxy."""


class GatewayStreamError(cc.CollabError):
    """A gateway stream ended without a provider completion envelope."""


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    virtual_key: str

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        key_env: str = "LITELLM_VIRTUAL_KEY",
    ) -> GatewayConfig:
        if key_env != "LITELLM_VIRTUAL_KEY":
            raise GatewayConfigError("gateway authentication must use LITELLM_VIRTUAL_KEY")
        base = (base_url or os.environ.get("LITELLM_BASE_URL") or "").strip().rstrip("/")
        if not base:
            raise GatewayConfigError("LITELLM_BASE_URL is required; direct-provider fallback is disabled")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise GatewayConfigError("LITELLM_BASE_URL must be an absolute http(s) URL")
        host = parsed.hostname.lower()
        if any(host == suffix or host.endswith("." + suffix) for suffix in _PROVIDER_HOST_SUFFIXES):
            raise GatewayConfigError(f"direct provider host {host!r} is forbidden; use the LiteLLM gateway")
        key = (os.environ.get("LITELLM_VIRTUAL_KEY") or "").strip()
        if not key:
            raise GatewayConfigError(
                "LITELLM_VIRTUAL_KEY is required; provider/master-key fallback is disabled"
            )
        if not base.endswith("/v1"):
            base += "/v1"
        return cls(base, key)

    def url(self, endpoint: str) -> str:
        endpoint = endpoint.strip("/")
        if endpoint not in ("chat/completions", "responses"):
            raise GatewayConfigError(f"unsupported gateway endpoint: {endpoint!r}")
        return f"{self.base_url}/{endpoint}"


def request_metadata(model_alias: str, *, feature: str) -> dict[str, Any]:
    service = (os.environ.get("SERVICE_NAME") or os.environ.get("LLG_SERVICE") or "ibkr-auto-trader").strip()
    environment = (
        os.environ.get("ENVIRONMENT") or os.environ.get("LLG_ENVIRONMENT") or "development"
    ).strip()
    release = (
        os.environ.get("GIT_SHA")
        or os.environ.get("RELEASE")
        or os.environ.get("LLG_RELEASE")
        or "dev"
    ).strip()
    name = f"{service}:{feature}"
    metadata: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "service": service,
        "feature": feature,
        "environment": environment,
        "release": release,
        "model_alias": model_alias,
        "trace_name": name,
        "generation_name": name,
        "trace_release": release,
        "tags": [
            f"environment:{environment}",
            f"service:{service}",
            f"feature:{feature}",
            f"model_alias:{model_alias}",
        ],
    }
    optional = {
        "parent_attempt_id": os.environ.get("COLLAB_EXECUTION_ID"),
        "session_id": os.environ.get("COLLAB_RUN_UID"),
        "trace_user_id": os.environ.get("TRACE_USER_ID"),
        "handoff_id": os.environ.get("COLLAB_HANDOFF_ID"),
        "run_uid": os.environ.get("COLLAB_RUN_UID"),
        "seat": os.environ.get("COLLAB_SEAT"),
        "candidate_id": os.environ.get("COLLAB_CANDIDATE_ID"),
        "escalation_id": os.environ.get("COLLAB_ESCALATION_ID"),
    }
    metadata.update({key: value for key, value in optional.items() if value})
    return metadata


def _telemetry_path() -> Path | None:
    explicit = (os.environ.get("COLLAB_MODEL_CALL_LOG") or "").strip()
    if explicit:
        return Path(explicit)
    collab = (os.environ.get("COLLAB_DIR") or "").strip()
    return Path(collab) / "autopilot" / "model-calls.jsonl" if collab else None


def _health_path(path: Path) -> Path:
    return path.with_name("model-calls-health.json")


def _write_health(
    path: Path, status: str, reason: str | None, *, run_uid: str | None = None
) -> None:
    try:
        failures = 0
        try:
            previous = json.loads(_health_path(path).read_text("utf-8"))
            failures = int(previous.get("telemetry_failures") or 0) if isinstance(previous, dict) else 0
        except (OSError, ValueError, TypeError):
            pass
        if status == "unavailable":
            failures += 1
        cc.safe_write(
            _health_path(path),
            json.dumps(
                {
                    "schema_version": "1.0",
                    "status": status,
                    "reason": reason,
                    "telemetry_failures": failures,
                    "updated_ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "run_uid": run_uid,
                },
                separators=(",", ":"),
            )
            + "\n",
        )
    except Exception:
        return


def _append_telemetry(record: dict[str, Any]) -> None:
    path = _telemetry_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            cc.collab_lock(path.with_name(path.name + ".lock"), ttl=10.0, acquire_timeout=30.0),
            path.open("a", encoding="utf-8", newline="\n") as stream,
        ):
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        _write_health(path, "healthy", None, run_uid=record.get("run_uid"))
    except Exception as exc:
        _write_health(
            path,
            "unavailable",
            f"{type(exc).__name__}: telemetry append failed"[:200],
            run_uid=record.get("run_uid"),
        )


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, InterruptedError):
        return "cancelled"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        return "gateway_unreachable"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, GatewayStreamError)):
        return "parsing_error"
    return "client_error"


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    return {
        "input": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output": usage.get("output_tokens", usage.get("completion_tokens")),
        "cached": details.get("cached_tokens"),
        "total": usage.get("total_tokens"),
    }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get(name)
    return str(value) if value else None


def _stream_lines(response: Any):
    """Read an SSE response, retaining a test-compatible buffered fallback."""
    if hasattr(response, "__iter__"):
        for line in response:
            if not isinstance(line, bytes):
                raise GatewayStreamError("gateway stream line must be bytes")
            yield line
        return
    raw = response.read()
    if not isinstance(raw, bytes):
        raise GatewayStreamError("gateway stream body must be bytes")
    yield from raw.splitlines()


def _decode_stream(
    response: Any,
    endpoint: str,
    *,
    started: float,
    on_progress: Callable[[int, float | None, str], None] | None = None,
) -> tuple[dict[str, Any], int, float | None, str | None]:
    frames: list[dict[str, Any]] = []
    first_token_latency_ms: float | None = None
    last_chunk_ts: str | None = None
    for raw_line in _stream_lines(response):
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        frame = json.loads(payload)
        if not isinstance(frame, dict):
            raise GatewayStreamError("gateway stream frame must be an object")
        frames.append(frame)
        choices = frame.get("choices")
        chat_content = (
            choices[0].get("delta", {}).get("content")
            if isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("delta"), dict)
            else None
        )
        responses_content = (
            frame.get("delta")
            if str(frame.get("type") or "").endswith(".delta")
            else None
        )
        content_bearing = bool(
            (isinstance(chat_content, str) and chat_content)
            or (isinstance(responses_content, str) and responses_content)
        )
        first_content = first_token_latency_ms is None and content_bearing
        if first_content:
            first_token_latency_ms = round((time.monotonic() - started) * 1000, 1)
        if content_bearing:
            last_chunk_ts = _timestamp()
            if on_progress is not None and (first_content or len(frames) % 25 == 0):
                on_progress(len(frames), first_token_latency_ms, last_chunk_ts)
    if not frames:
        raise GatewayStreamError("gateway stream contained no data frames")

    if endpoint == "responses":
        for frame in reversed(frames):
            if frame.get("type") == "response.completed" and isinstance(
                frame.get("response"), dict
            ):
                return frame["response"], len(frames), first_token_latency_ms, last_chunk_ts
        raise GatewayStreamError("Responses API stream ended without response.completed")

    content: list[str] = []
    finish_reason: Any = None
    last: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    hidden: dict[str, Any] = {}
    for frame in frames:
        last = frame
        if isinstance(frame.get("usage"), dict):
            usage = frame["usage"]
        if isinstance(frame.get("_hidden_params"), dict):
            hidden = frame["_hidden_params"]
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            content.append(delta["content"])
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    if not last.get("id"):
        raise GatewayStreamError("chat completions stream ended without a provider response id")
    result: dict[str, Any] = {
        "id": last["id"],
        "model": last.get("model"),
        "choices": [
            {
                "message": {"role": "assistant", "content": "".join(content)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    if last.get("provider") is not None:
        result["provider"] = last["provider"]
    if hidden:
        result["_hidden_params"] = hidden
    return result, len(frames), first_token_latency_ms, last_chunk_ts


def _model_event(
    metadata: dict[str, Any],
    *,
    state: mo.LifecycleState,
    requested_model: Any,
    endpoint: str,
    **updates: Any,
) -> mo.ModelAttemptEvent:
    return mo.ModelAttemptEvent(
        event_id=str(uuid.uuid4()),
        attempt_id=str(metadata.get("attempt_id") or metadata.get("request_id") or uuid.uuid4()),
        request_id=str(metadata.get("request_id") or uuid.uuid4()),
        run_uid=metadata.get("run_uid"),
        seat=metadata.get("seat"),
        requested_model=str(requested_model) if requested_model else None,
        state=state,
        event_ts=_timestamp(),
        attempt_number=int(metadata.get("attempt_number") or metadata.get("retry") or 0) + 1,
        source="gateway_client",
        parent_attempt_id=metadata.get("parent_attempt_id"),
        handoff_id=metadata.get("handoff_id"),
        candidate_id=metadata.get("candidate_id"),
        escalation_id=metadata.get("escalation_id"),
        gateway_route=endpoint,
        streaming=bool(updates.pop("streaming", False)),
        retry_count=int(metadata.get("retry") or 0),
        **updates,
    )


def post_json(
    url: str,
    key: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    opener=urllib.request.urlopen,
) -> dict[str, Any]:
    """POST through the validated gateway and retain one redacted record per HTTP attempt."""
    base_url = (
        url.rsplit("/", 1)[0]
        if url.endswith("/responses")
        else url.rsplit("/chat/completions", 1)[0]
    )
    config = GatewayConfig.from_env(base_url=base_url)
    if key != config.virtual_key:
        raise GatewayConfigError("request key is not the configured LITELLM_VIRTUAL_KEY")
    endpoint = "responses" if url.endswith("/responses") else "chat/completions"
    expected = config.url(endpoint)
    if url.rstrip("/") != expected:
        raise GatewayConfigError("request URL does not match the configured LiteLLM gateway")
    raw_metadata = payload.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    started_wall = datetime.now(UTC)
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "record_type": "attempt",
        "event_id": str(uuid.uuid4()),
        "request_id": metadata.get("request_id"),
        "handoff_id": metadata.get("handoff_id"),
        "run_uid": metadata.get("run_uid"),
        "seat": metadata.get("seat"),
        "candidate_id": metadata.get("candidate_id"),
        "escalation_id": metadata.get("escalation_id"),
        "trace_name": metadata.get("trace_name"),
        "session_id": metadata.get("session_id"),
        "environment": metadata.get("environment"),
        "release": metadata.get("release"),
        "endpoint": endpoint,
        "requested_model": payload.get("model"),
        "actual_model": None,
        "provider": None,
        "started_ts": started_wall.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "ended_ts": None,
        "first_token_latency_ms": None,
        "total_latency_ms": None,
        "tokens": {"input": None, "output": None, "cached": None, "total": None},
        "cost": None,
        "retry": int(metadata.get("retry") or 0),
        "streaming": bool(payload.get("stream")),
        "completion_status": "interrupted",
        "outcome": "client_error",
        "langfuse_export": "unverified",
    }
    if int(metadata.get("retry") or 0) > 0:
        mo.append_if_configured(
            _model_event(
                metadata,
                state="retrying",
                requested_model=payload.get("model"),
                endpoint=endpoint,
                streaming=bool(payload.get("stream")),
            )
        )
    mo.append_if_configured(
        _model_event(
            metadata,
            state="connecting",
            requested_model=payload.get("model"),
            endpoint=endpoint,
            streaming=bool(payload.get("stream")),
        )
    )
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with opener(request, timeout=timeout) as response:
            gateway_request_id = _response_header(response, "x-litellm-request-id") or _response_header(
                response, "x-request-id"
            )
            mo.append_if_configured(
                _model_event(
                    metadata,
                    state="gateway_accepted",
                    requested_model=payload.get("model"),
                    endpoint=endpoint,
                    streaming=bool(payload.get("stream")),
                    gateway_request_id=gateway_request_id,
                )
            )
            chunk_count: int | None = None
            first_token_latency_ms: float | None = None
            if payload.get("stream"):
                mo.append_if_configured(
                    _model_event(
                        metadata,
                        state="streaming",
                        requested_model=payload.get("model"),
                        endpoint=endpoint,
                        streaming=True,
                        gateway_request_id=gateway_request_id,
                        detail={"phase": "response_started"},
                    )
                )

                def retain_progress(count: int, latency: float | None, last_chunk_ts: str) -> None:
                    mo.append_if_configured(
                        _model_event(
                            metadata,
                            state="streaming",
                            requested_model=payload.get("model"),
                            endpoint=endpoint,
                            streaming=True,
                            gateway_request_id=gateway_request_id,
                            first_token_latency_ms=latency,
                            detail={
                                "phase": "response_in_progress",
                                "chunk_count": count,
                                "last_chunk_ts": last_chunk_ts,
                            },
                        )
                    )

                data, chunk_count, first_token_latency_ms, last_chunk_ts = _decode_stream(
                    response,
                    endpoint,
                    started=started,
                    on_progress=retain_progress,
                )
            else:
                mo.append_if_configured(
                    _model_event(
                        metadata,
                        state="generating",
                        requested_model=payload.get("model"),
                        endpoint=endpoint,
                        streaming=False,
                        gateway_request_id=gateway_request_id,
                        detail={"phase": "waiting_for_provider_response"},
                    )
                )
                data = json.loads(response.read())
                last_chunk_ts = None
        if not isinstance(data, dict):
            raise TypeError("gateway response must be an object")
        record.update(
            {
                "actual_model": data.get("model"),
                "provider": data.get("provider")
                or (data.get("_hidden_params") or {}).get("custom_llm_provider"),
                "tokens": _usage(data),
                "cost": (data.get("_hidden_params") or {}).get("response_cost"),
                "first_token_latency_ms": first_token_latency_ms,
                "completion_status": "completed",
                "outcome": "success",
            }
        )
        completion_detail: dict[str, Any] = {}
        if chunk_count is not None:
            completion_detail = {"phase": "response_complete", "chunk_count": chunk_count}
            if last_chunk_ts is not None:
                completion_detail["last_chunk_ts"] = last_chunk_ts
        mo.append_if_configured(
            _model_event(
                metadata,
                state="completed",
                requested_model=payload.get("model"),
                endpoint=endpoint,
                streaming=bool(payload.get("stream")),
                gateway_request_id=gateway_request_id,
                provider_request_id=str(data.get("id")) if data.get("id") else None,
                actual_model=data.get("model"),
                provider=record["provider"],
                completion_status="completed",
                first_token_latency_ms=first_token_latency_ms,
                tokens=record["tokens"],
                cost=record["cost"],
                total_duration_ms=round((time.monotonic() - started) * 1000, 1),
                detail=completion_detail,
            )
        )
        return data
    except Exception as exc:
        record["outcome"] = _classify(exc)
        terminal_state: mo.LifecycleState = (
            "timed_out"
            if record["outcome"] == "timeout"
            else "cancelled"
            if record["outcome"] == "cancelled"
            else "failed"
        )
        mo.append_if_configured(
            _model_event(
                metadata,
                state=terminal_state,
                requested_model=payload.get("model"),
                endpoint=endpoint,
                streaming=bool(payload.get("stream")),
                completion_status="interrupted",
                failure_classification=record["outcome"],
                total_duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        )
        raise
    finally:
        record["ended_ts"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record["total_latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        _append_telemetry(record)


def record_langfuse_verification(
    request_id: str,
    *,
    observation_id: str,
    trace_id: str | None,
    status: str = "verified",
) -> None:
    """Append Cloud verification without rewriting the attempt's point-in-time evidence."""
    if status not in ("verified", "rejected"):
        raise ValueError("Langfuse verification status must be verified or rejected")
    if not request_id or not observation_id:
        raise ValueError("request_id and observation_id are required")
    path = mo.event_log_path()
    prior = mo.find_by_request(path, request_id) if path is not None else None
    mo.append_if_configured(
        mo.ModelAttemptEvent(
            event_id=str(uuid.uuid4()),
            attempt_id=prior.attempt_id if prior else request_id,
            request_id=request_id,
            run_uid=prior.run_uid if prior else None,
            seat=prior.seat if prior else None,
            requested_model=prior.requested_model if prior else None,
            state="telemetry_verified" if status == "verified" else "telemetry_failed",
            event_ts=_timestamp(),
            attempt_number=prior.attempt_number if prior else 1,
            source="langfuse_reconciler",
            handoff_id=prior.handoff_id if prior else None,
            candidate_id=prior.candidate_id if prior else None,
            escalation_id=prior.escalation_id if prior else None,
            retry_count=prior.retry_count if prior else 0,
            telemetry_result=status,
            observation_id=observation_id,
            trace_id=trace_id,
        )
    )
    _append_telemetry(
        {
            "schema_version": "1.0",
            "record_type": "langfuse_verification",
            "event_id": str(uuid.uuid4()),
            "request_id": request_id,
            "observation_id": observation_id,
            "trace_id": trace_id,
            "verified_ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "langfuse_export": status,
        }
    )
