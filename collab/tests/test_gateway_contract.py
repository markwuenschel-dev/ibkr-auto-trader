"""Fail-closed LiteLLM routing, Langfuse metadata, and redacted attempt telemetry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import llm_gateway as gw  # noqa: E402


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
    monkeypatch.setenv("COLLAB_MODEL_CALL_LOG", str(log))
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


def test_adapter_sources_have_no_direct_provider_defaults_or_provider_key_selection() -> None:
    adapters = Path(__file__).resolve().parent.parent / "tools" / "adapters"
    for name in ("openai-compatible-seat.py", "openai-repo-seat.py"):
        source = (adapters / name).read_text("utf-8")
        assert "https://api.openai.com" not in source
        assert "https://api.x.ai" not in source
        assert 'default="OPENAI_API_KEY"' not in source
        assert 'default="SEAT_API_KEY"' not in source
