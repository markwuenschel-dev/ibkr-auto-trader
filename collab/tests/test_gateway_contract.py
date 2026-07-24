"""Fail-closed LiteLLM routing, Langfuse metadata, and redacted attempt telemetry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import llm_gateway as gw  # noqa: E402
import model_observability as mo  # noqa: E402
import seat_gateway_policy as sgp  # noqa: E402


def test_config_requires_virtual_key_and_rejects_direct_provider(monkeypatch) -> None:
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_VIRTUAL_KEY", raising=False)
    with pytest.raises(gw.GatewayConfigError, match="LITELLM_BASE_URL"):
        gw.GatewayConfig.from_env()

    monkeypatch.setenv("LITELLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    with pytest.raises(gw.GatewayConfigError, match="direct provider"):
        gw.GatewayConfig.from_env()

    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    cfg = gw.GatewayConfig.from_env()
    assert cfg.base_url == "http://127.0.0.1:4000/v1"
    with pytest.raises(gw.GatewayConfigError, match="LITELLM_VIRTUAL_KEY"):
        gw.GatewayConfig.from_env(key_env="OPENAI_API_KEY")


def test_metadata_contains_native_langfuse_dimensions_and_work_correlation(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "ibkr-auto-trader")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("COLLAB_HANDOFF_ID", "035")
    monkeypatch.setenv("COLLAB_RUN_UID", "run-7")
    monkeypatch.setenv("COLLAB_SEAT", "reviewer")
    monkeypatch.setenv("COLLAB_CANDIDATE_ID", "candidate-9")
    monkeypatch.setenv("COLLAB_ESCALATION_ID", "esc-2")
    meta = gw.request_metadata("gpt-5.6-luna", feature="repo-seat")
    assert meta["trace_name"] == "ibkr-auto-trader:repo-seat"
    assert meta["generation_name"] == "ibkr-auto-trader:repo-seat"
    assert meta["trace_release"] == "abc123"
    assert meta["session_id"] == "run-7"
    assert meta["handoff_id"] == "035" and meta["seat"] == "reviewer"
    assert meta["candidate_id"] == "candidate-9" and meta["escalation_id"] == "esc-2"
    assert set(meta["tags"]) == {
        "environment:test",
        "service:ibkr-auto-trader",
        "feature:repo-seat",
        "model_alias:gpt-5.6-luna",
    }


def test_post_records_redacted_success_and_classified_failure(monkeypatch, tmp_path: Path) -> None:
    log = tmp_path / "model-calls.jsonl"
    event_log = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_CALL_LOG", str(log))
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(event_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-super-secret")
    cfg = gw.GatewayConfig.from_env()
    payload = {
        "model": "gpt-5.6-luna",
        "input": "PRIVATE PROMPT",
        "metadata": gw.request_metadata("gpt-5.6-luna", feature="repo-seat"),
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    response = Response()
    response.read = lambda: json.dumps(
        {
            "model": "openai/gpt-5.6-luna-2026-07-01",
            "provider": "openai",
            "output_text": "PRIVATE COMPLETION",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80},
            },
            "_hidden_params": {"response_cost": 0.012},
        }
    ).encode()
    result = gw.post_json(
        cfg.url("responses"), cfg.virtual_key, payload, 5, opener=lambda *args, **kwargs: response
    )
    assert result["output_text"] == "PRIVATE COMPLETION"

    def timeout(*args, **kwargs):
        raise TimeoutError("timed out with sk-super-secret")

    with pytest.raises(TimeoutError):
        gw.post_json(cfg.url("responses"), cfg.virtual_key, payload, 5, opener=timeout)
    gw.record_langfuse_verification(
        payload["metadata"]["request_id"], observation_id="obs-1", trace_id="trace-1"
    )
    records = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert [record["outcome"] for record in records[:2]] == ["success", "timeout"]
    assert records[0]["requested_model"] == "gpt-5.6-luna"
    assert records[0]["actual_model"] == "openai/gpt-5.6-luna-2026-07-01"
    assert records[0]["provider"] == "openai" and records[0]["cost"] == 0.012
    assert records[0]["tokens"] == {"input": 100, "output": 20, "cached": 80, "total": 120}
    assert records[0]["langfuse_export"] == "unverified"
    assert records[2]["record_type"] == "langfuse_verification"
    assert records[2]["langfuse_export"] == "verified"
    encoded = json.dumps(records)
    assert "PRIVATE PROMPT" not in encoded and "PRIVATE COMPLETION" not in encoded
    assert "sk-super-secret" not in encoded

    events = mo.read_events(event_log)
    first_attempt = [event for event in events if event.request_id == payload["metadata"]["request_id"]]
    assert [event.state for event in first_attempt] == [
        "connecting",
        "gateway_accepted",
        "generating",
        "completed",
        "connecting",
        "timed_out",
        "telemetry_verified",
    ]
    assert first_attempt[0].run_uid is None
    assert first_attempt[3].actual_model == "openai/gpt-5.6-luna-2026-07-01"
    assert first_attempt[3].provider == "openai"
    assert first_attempt[5].failure_classification == "timeout"
    event_encoded = event_log.read_text("utf-8")
    assert "PRIVATE PROMPT" not in event_encoded and "PRIVATE COMPLETION" not in event_encoded
    assert "sk-super-secret" not in event_encoded


def test_model_attempt_is_visible_before_gateway_returns(monkeypatch, tmp_path: Path) -> None:
    event_log = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(event_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    monkeypatch.setenv("COLLAB_RUN_UID", "run-live")
    cfg = gw.GatewayConfig.from_env()
    payload = {
        "model": "gpt-5.6-luna",
        "input": "PRIVATE PROMPT",
        "metadata": gw.request_metadata("gpt-5.6-luna", feature="repo-seat"),
    }

    class Response:
        def __init__(self) -> None:
            self.headers = {"x-litellm-request-id": "litellm-1"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            visible = mo.read_events(event_log)
            assert [event.state for event in visible] == [
                "connecting",
                "gateway_accepted",
                "generating",
            ]
            return json.dumps(
                {
                    "id": "provider-1",
                    "model": "openai/gpt-5.6-luna",
                    "provider": "openai",
                    "output_text": "PRIVATE COMPLETION",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            ).encode()

    def opener(*args, **kwargs):
        visible = mo.read_events(event_log)
        assert [(event.state, event.run_uid) for event in visible] == [("connecting", "run-live")]
        return Response()

    gw.post_json(cfg.url("responses"), cfg.virtual_key, payload, 5, opener=opener)
    events = mo.read_events(event_log)
    assert [event.state for event in events] == [
        "connecting",
        "gateway_accepted",
        "generating",
        "completed",
    ]
    assert events[1].gateway_request_id == "litellm-1"
    assert events[3].provider_request_id == "provider-1"


@pytest.mark.parametrize(
    ("alias", "actual_model", "provider", "endpoint"),
    [
        ("gpt-5.6-luna", "openai/gpt-5.6-luna", "openai", "responses"),
        ("grok-4.5", "xai/grok-4.5", "xai", "responses"),
        ("gemini-3.5-flash", "gemini/gemini-3.5-flash", "google", "responses"),
        ("haiku-4.5", "anthropic/claude-haiku-4-5", "anthropic", "chat/completions"),
    ],
)
def test_each_supported_alias_crosses_the_canonical_litellm_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    alias: str,
    actual_model: str,
    provider: str,
    endpoint: str,
) -> None:
    event_log = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(event_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    config = gw.GatewayConfig.from_env()
    payload = {"model": alias, "metadata": gw.request_metadata(alias, feature="alias-contract")}
    seen_urls: list[str] = []

    class Response:
        def __init__(self) -> None:
            self.headers = {"x-litellm-request-id": f"litellm-{alias}"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": f"provider-{alias}",
                    "model": actual_model,
                    "provider": provider,
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            ).encode()

    def opener(request, **kwargs):
        seen_urls.append(request.full_url)
        return Response()

    result = gw.post_json(config.url(endpoint), config.virtual_key, payload, 5, opener=opener)
    assert seen_urls == [config.url(endpoint)]
    assert result["model"] == actual_model
    attempt = mo.reduce_attempts(mo.read_events(event_log))[0]
    assert attempt["requested_model"] == alias
    assert attempt["actual_model"] == actual_model
    assert attempt["provider"] == provider
    assert attempt["gateway_request_id"] == f"litellm-{alias}"


def test_attempt_telemetry_write_failure_is_visible_without_losing_provider_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    call_log = tmp_path / "model-calls.jsonl"
    call_log.mkdir()
    monkeypatch.setenv("COLLAB_MODEL_CALL_LOG", str(call_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    config = gw.GatewayConfig.from_env()
    payload = {
        "model": "gpt-5.6-luna",
        "metadata": gw.request_metadata("gpt-5.6-luna", feature="persistence-failure"),
    }

    class Response:
        headers = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"id": "provider-1", "model": "openai/gpt-5.6-luna", "provider": "openai"}
            ).encode()

    result = gw.post_json(
        config.url("responses"),
        config.virtual_key,
        payload,
        5,
        opener=lambda *args, **kwargs: Response(),
    )
    assert result["id"] == "provider-1"
    health = json.loads((tmp_path / "model-calls-health.json").read_text("utf-8"))
    assert health["status"] == "unavailable"
    assert health["telemetry_failures"] == 1
    assert health["reason"].endswith(": telemetry append failed")
    assert str(tmp_path) not in health["reason"]


@pytest.mark.parametrize("endpoint", ["responses", "chat/completions"])
def test_provider_specific_streaming_completion_is_retained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, endpoint: str
) -> None:
    event_log = tmp_path / "model-events.jsonl"
    monkeypatch.setenv("COLLAB_MODEL_EVENT_LOG", str(event_log))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
    config = gw.GatewayConfig.from_env()
    metadata = gw.request_metadata("gemini-3.5-flash", feature="stream-test")
    payload = {"model": "gemini-3.5-flash", "stream": True, "metadata": metadata}

    if endpoint == "responses":
        frames = [
            {"type": "response.output_text.delta", "delta": "hel"},
            {
                "type": "response.completed",
                "response": {
                    "id": "provider-responses-1",
                    "model": "gemini-3.5-flash",
                    "output_text": "hello",
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                },
            },
        ]
    else:
        frames = [
            {
                "id": "provider-chat-1",
                "model": "gemini-3.5-flash",
                "choices": [{"delta": {"content": "hel"}, "finish_reason": None}],
            },
            {
                "id": "provider-chat-1",
                "model": "gemini-3.5-flash",
                "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        ]
    raw = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"

    class Response:
        def __init__(self) -> None:
            self.headers = {"x-litellm-request-id": "litellm-stream-1"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return raw.encode()

        def __iter__(self):
            return iter(raw.encode().splitlines())

    result = gw.post_json(
        config.url(endpoint),
        config.virtual_key,
        payload,
        5,
        opener=lambda *args, **kwargs: Response(),
    )
    assert result["model"] == "gemini-3.5-flash"
    if endpoint == "responses":
        assert result["output_text"] == "hello"
    else:
        assert result["choices"][0]["message"]["content"] == "hello"
    events = mo.read_events(event_log)
    assert [event.state for event in events] == [
        "connecting",
        "gateway_accepted",
        "streaming",
        "streaming",
        "completed",
    ]
    assert events[-1].detail["chunk_count"] == 2
    assert events[-1].detail["phase"] == "response_complete"
    assert events[-1].detail["last_chunk_ts"]
    assert events[-2].detail["phase"] == "response_in_progress"
    assert events[-2].detail["chunk_count"] == 1
    assert events[-1].first_token_latency_ms is not None


def test_adapter_sources_have_no_direct_provider_defaults_or_provider_key_selection() -> None:
    adapters = Path(__file__).resolve().parent.parent / "tools" / "adapters"
    for name in ("openai-compatible-seat.py", "openai-repo-seat.py"):
        source = (adapters / name).read_text("utf-8")
        assert "https://api.openai.com" not in source
        assert "https://api.x.ai" not in source
        assert 'default="OPENAI_API_KEY"' not in source
        assert 'default="SEAT_API_KEY"' not in source


def test_example_routes_every_in_scope_seat_through_gateway_adapter() -> None:
    example = Path(__file__).resolve().parent.parent / "seats.example.json"
    doc = json.loads(example.read_text("utf-8"))
    assert sgp.validate_gateway_seats(doc) == []
    assert doc["seats"]["verifier"]["model"] == "haiku-4.5"
    assert doc["models"]["haiku-4.5"]["provider"] == "anthropic"


def test_gateway_seat_policy_rejects_direct_provider_command() -> None:
    doc = {
        "models": {
            "haiku-4.5": {
                "provider": "anthropic",
                "cmd": ["claude", "-p", "--model", "claude-haiku-4-5"],
            }
        },
        "seats": {
            "verifier": {"backend": "cli", "role": "verifier", "model": "haiku-4.5"}
        },
    }
    errors = sgp.validate_gateway_seats(doc)
    assert errors == [
        "seat 'verifier' model 'haiku-4.5' bypasses the approved LiteLLM gateway adapters"
    ]
