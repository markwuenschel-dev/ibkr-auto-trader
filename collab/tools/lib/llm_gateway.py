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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collab_common as cc

_PROVIDER_HOST_SUFFIXES = (
    "api.openai.com",
    "api.x.ai",
    "anthropic.com",
    "googleapis.com",
    "generativelanguage.googleapis.com",
)


class GatewayConfigError(cc.CollabError):
    """Gateway configuration is absent or could bypass the controlled proxy."""


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


def _write_health(path: Path, status: str, reason: str | None) -> None:
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
        _write_health(path, "healthy", None)
    except Exception as exc:
        _write_health(path, "unavailable", f"{type(exc).__name__}: telemetry append failed"[:200])


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        return "gateway_unreachable"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "parsing_error"
    return "client_error"


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    return {
        "input": usage.get("input_tokens", usage.get("prompt_tokens")),
        "output": usage.get("output_tokens", usage.get("completion_tokens")),
        "cached": details.get("cached_tokens"),
        "total": usage.get("total_tokens"),
    }


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
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
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
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise TypeError("gateway response must be an object")
        record.update(
            {
                "actual_model": data.get("model"),
                "provider": data.get("provider")
                or (data.get("_hidden_params") or {}).get("custom_llm_provider"),
                "tokens": _usage(data),
                "cost": (data.get("_hidden_params") or {}).get("response_cost"),
                "completion_status": "completed",
                "outcome": "success",
            }
        )
        return data
    except Exception as exc:
        record["outcome"] = _classify(exc)
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
