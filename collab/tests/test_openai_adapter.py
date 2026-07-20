"""Tests for tools/adapters/openai-compatible-seat.py — the OpenAI-compatible autopilot backend.

No network: the HTTP layer (``_post_json``) is monkeypatched. Covers chat vs Responses API selection,
the auto-fallback on the "use /responses" 404, response-shape parsing, and the missing-key contract.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

_ADAPTER = Path(__file__).resolve().parent.parent / "tools" / "adapters" / "openai-compatible-seat.py"


def _load():
    spec = importlib.util.spec_from_file_location("seat_adapter", _ADAPTER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


seat = _load()


class TestResponsesParsing:
    def test_extract_from_output_array_skips_reasoning(self):
        data = {
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello world"}],
                },
            ]
        }
        assert seat._extract_responses_text(data) == "hello world"

    def test_extract_prefers_output_text_field(self):
        assert seat._extract_responses_text({"output_text": "quick", "output": []}) == "quick"

    def test_extract_empty_when_no_text(self):
        assert seat._extract_responses_text({"output": [{"type": "reasoning", "content": []}]}) == ""


class TestApiSelection:
    def _run(self, monkeypatch, capsys, argv, post, stdin="PROMPT"):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("SEAT_ENV_FILE", str(_ADAPTER.parent / "does-not-exist.env"))  # skip the real .env
        monkeypatch.setattr(seat.sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(seat, "_post_json", post)
        rc = seat.main(argv)
        return rc, capsys.readouterr()

    def test_auto_falls_back_to_responses_on_404(self, monkeypatch, capsys):
        def post(url, key, payload, timeout):
            if url.endswith("/chat/completions"):
                raise urllib.error.HTTPError(
                    url, 404, "Not Found", {}, io.BytesIO(b"Use the v1/responses endpoint instead")
                )
            assert url.endswith("/responses") and payload["input"] == "PROMPT"
            return {"output_text": "from responses"}

        rc, cap = self._run(
            monkeypatch,
            capsys,
            ["--base", "https://api.openai.com/v1", "--model", "gpt-5-codex", "--key-env", "OPENAI_API_KEY"],
            post,
        )
        assert rc == 0 and cap.out == "from responses"

    def test_chat_mode_success(self, monkeypatch, capsys):
        def post(url, key, payload, timeout):
            assert url.endswith("/chat/completions")
            return {"choices": [{"message": {"content": "chat reply"}}]}

        rc, cap = self._run(monkeypatch, capsys, ["--api", "chat", "--key-env", "OPENAI_API_KEY"], post)
        assert rc == 0 and cap.out == "chat reply"

    def test_responses_mode_explicit(self, monkeypatch, capsys):
        def post(url, key, payload, timeout):
            assert url.endswith("/responses")
            return {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "resp reply"}]}]
            }

        rc, cap = self._run(monkeypatch, capsys, ["--api", "responses", "--key-env", "OPENAI_API_KEY"], post)
        assert rc == 0 and cap.out == "resp reply"

    def test_non_responses_404_does_not_fall_back(self, monkeypatch, capsys):
        def post(url, key, payload, timeout):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"no such model"))

        rc, cap = self._run(monkeypatch, capsys, ["--key-env", "OPENAI_API_KEY"], post)
        assert rc == 1 and "api error 404" in cap.err

    def test_missing_key_returns_2(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("SEAT_ENV_FILE", str(_ADAPTER.parent / "does-not-exist.env"))
        monkeypatch.setattr(seat.sys, "stdin", io.StringIO("x"))
        assert seat.main(["--key-env", "OPENAI_API_KEY"]) == 2


class TestPromptCaching:
    """Prompt-caching slice (2026-07-20 memo): stable prompt_cache_key on both single-shot payloads
    plus per-POST usage telemetry (stderr + optional JSONL)."""

    def _run(self, monkeypatch, capsys, argv, post, stdin="PROMPT"):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("SEAT_ENV_FILE", str(_ADAPTER.parent / "does-not-exist.env"))
        monkeypatch.setattr(seat.sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(seat, "_post_json", post)
        rc = seat.main(argv)
        return rc, capsys.readouterr()

    def test_chat_carries_cache_key_and_logs_usage(self, monkeypatch, capsys):
        seen: dict = {}

        def post(url, key, payload, timeout):
            seen.update(payload)
            return {
                "choices": [{"message": {"content": "r"}}],
                "usage": {"prompt_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 1024}},
            }

        rc, cap = self._run(monkeypatch, capsys, ["--api", "chat", "--key-env", "OPENAI_API_KEY"], post)
        assert rc == 0 and cap.out == "r"
        assert seen["prompt_cache_key"] == seat._PROMPT_CACHE_KEY
        assert "usage api=chat step=1 prompt_tokens=1200 cached_tokens=1024" in cap.err

    def test_responses_carries_cache_key_and_writes_jsonl(self, monkeypatch, capsys, tmp_path):
        log = tmp_path / "usage.jsonl"
        monkeypatch.setenv("COLLAB_USAGE_LOG", str(log))

        def post(url, key, payload, timeout):
            assert payload["prompt_cache_key"] == seat._PROMPT_CACHE_KEY
            return {
                "output_text": "r",
                "usage": {"input_tokens": 1100, "input_tokens_details": {"cached_tokens": 0}},
            }

        rc, cap = self._run(
            monkeypatch, capsys, ["--api", "responses", "--key-env", "OPENAI_API_KEY"], post
        )
        assert rc == 0 and cap.out == "r"
        (rec,) = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        assert rec["adapter"] == "compatible-seat" and rec["api"] == "responses"
        assert rec["cached_tokens"] == 0 and rec["prompt_tokens"] == 1100

    def test_absent_usage_logs_na(self, monkeypatch, capsys):
        def post(url, key, payload, timeout):
            return {"choices": [{"message": {"content": "r"}}]}

        rc, cap = self._run(monkeypatch, capsys, ["--api", "chat", "--key-env", "OPENAI_API_KEY"], post)
        assert rc == 0
        assert "prompt_tokens=n/a cached_tokens=n/a cache_write_tokens=n/a" in cap.err

    def test_xai_sticky_header_only_on_xai_hosts(self):
        h = seat._request_headers("https://api.x.ai/v1/chat/completions", "k")
        assert h["x-grok-conv-id"] == seat._PROMPT_CACHE_KEY
        assert "x-grok-conv-id" not in seat._request_headers("https://api.openai.com/v1/responses", "k")
