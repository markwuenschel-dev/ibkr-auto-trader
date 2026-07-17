"""Tests for tools/adapters/openai-repo-seat.py — gpt-5.4+ tools → /responses bridge.

No network: HTTP is monkeypatched. Pins the 2026-07-17 luna failure mode:
function tools + reasoning_effort on /chat/completions must never be the first hop for gpt-5.4+.
"""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

_ADAPTER = Path(__file__).resolve().parent.parent / "tools" / "adapters" / "openai-repo-seat.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_repo_seat", _ADAPTER)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


seat = _load()


class TestBridgePredicate:
    def test_gpt54_plus_with_tools_bridges_without_reasoning_fields(self):
        # THE FIX: was gated on reasoning_effort is not None; that missed platform-injected reasoning.
        assert seat._should_bridge_to_responses("gpt-5.6-luna", has_tools=True) is True
        assert seat._should_bridge_to_responses("gpt-5.4", has_tools=True) is True
        assert seat._should_bridge_to_responses("gpt-5.5-build", has_tools=True) is True

    def test_gpt54_plus_without_tools_does_not_bridge(self):
        assert seat._should_bridge_to_responses("gpt-5.6-luna", has_tools=False) is False

    def test_older_gpt_with_tools_does_not_auto_bridge(self):
        assert seat._should_bridge_to_responses("gpt-5.3", has_tools=True) is False
        assert seat._should_bridge_to_responses("gpt-4o", has_tools=True) is False
        assert seat._should_bridge_to_responses("gpt-5-codex", has_tools=True) is False

    def test_explicit_reasoning_pair_bridges_even_on_older_models(self):
        # Original arm: both reasoning fields set → responses regardless of family.
        assert (
            seat._should_bridge_to_responses(
                "gpt-4o",
                has_tools=False,
                reasoning_effort="medium",
                reasoning_summary="auto",
            )
            is True
        )

    def test_one_reasoning_field_alone_does_not_bridge_older_model(self):
        assert (
            seat._should_bridge_to_responses(
                "gpt-4o", has_tools=False, reasoning_effort="medium", reasoning_summary=None
            )
            is False
        )


class TestChatRejectClassifier:
    def test_luna_400_is_retryable(self):
        body = (
            '{"error":{"message":"Function tools with reasoning_effort are not supported for '
            'gpt-5.6-luna in /v1/chat/completions. To use function tools, use /v1/responses or '
            'set reasoning_effort to \'none\'.","type":"invalid_request_error",'
            '"param":"reasoning_effort","code":null}}'
        )
        assert seat._chat_rejects_tools_for_responses(400, body) is True

    def test_404_responses_hint_is_retryable(self):
        assert seat._chat_rejects_tools_for_responses(404, "Use the v1/responses endpoint") is True

    def test_plain_400_is_not_retryable(self):
        assert seat._chat_rejects_tools_for_responses(400, '{"error":{"message":"bad request"}}') is False


class TestAutoSelectsResponses:
    def _run(self, monkeypatch, capsys, argv, post, tmp_path, stdin="PROMPT"):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("SEAT_ENV_FILE", str(tmp_path / "no.env"))
        monkeypatch.setattr(seat.sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(seat, "_post_json", post)
        rc = seat.main(["--repo-root", str(tmp_path), "--key-env", "OPENAI_API_KEY", *argv])
        return rc, capsys.readouterr()

    def test_auto_luna_run_checks_never_hits_chat(self, monkeypatch, capsys, tmp_path):
        hits: list[str] = []

        def post(url, key, payload, timeout):
            hits.append(url)
            if url.endswith("/chat/completions"):
                raise AssertionError("gpt-5.6-luna+tools must not call /chat/completions first")
            assert url.endswith("/responses")
            return {"output_text": "ok from responses"}

        rc, cap = self._run(
            monkeypatch,
            capsys,
            ["--model", "gpt-5.6-luna", "--run-checks", "--api", "auto"],
            post,
            tmp_path,
        )
        assert rc == 0 and cap.out == "ok from responses"
        assert hits and all(h.endswith("/responses") for h in hits)
        assert "bridging" in cap.err

    def test_auto_falls_back_on_reasoning_effort_400(self, monkeypatch, capsys, tmp_path):
        # Non-5.4 model: chat first; on the exact luna-shaped 400, retry responses.
        body = (
            b'{"error":{"message":"Function tools with reasoning_effort are not supported '
            b'for gpt-5.3 in /v1/chat/completions. use /v1/responses","param":"reasoning_effort"}}'
        )

        def post(url, key, payload, timeout):
            if url.endswith("/chat/completions"):
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))
            assert url.endswith("/responses")
            return {"output_text": "recovered"}

        rc, cap = self._run(
            monkeypatch,
            capsys,
            ["--model", "gpt-5.3", "--run-checks", "--api", "auto"],
            post,
            tmp_path,
        )
        assert rc == 0 and cap.out == "recovered"
        assert "retrying /responses" in cap.err
