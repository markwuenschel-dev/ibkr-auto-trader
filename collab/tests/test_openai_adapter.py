"""Tests for tools/adapters/openai-compatible-seat.py — the OpenAI-compatible autopilot backend.

No network: the HTTP layer (``_post_json``) is monkeypatched. Covers chat vs Responses API selection,
the auto-fallback on the "use /responses" 404, response-shape parsing, and the missing-key contract.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

import pytest

_ADAPTER = Path(__file__).resolve().parent.parent / "tools" / "adapters" / "openai-compatible-seat.py"


def _load():
    spec = importlib.util.spec_from_file_location("seat_adapter", _ADAPTER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


seat = _load()


class TestResponsesParsing:
    def test_extract_from_output_array_skips_reasoning(self):
        data = {"output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello world"}]},
        ]}
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
                    url, 404, "Not Found", {}, io.BytesIO(b"Use the v1/responses endpoint instead"))
            assert url.endswith("/responses") and payload["input"] == "PROMPT"
            return {"output_text": "from responses"}

        rc, cap = self._run(monkeypatch, capsys, [
            "--base", "https://api.openai.com/v1", "--model", "gpt-5-codex", "--key-env", "OPENAI_API_KEY"], post)
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
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "resp reply"}]}]}

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
