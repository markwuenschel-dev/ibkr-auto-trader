"""Tests for tools/adapters/openai-repo-seat.py — gpt-5.4+ tools → /responses bridge.

No network: HTTP is monkeypatched. Pins the 2026-07-17 luna failure mode:
function tools + reasoning_effort on /chat/completions must never be the first hop for gpt-5.4+.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
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
        monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "sk-virtual")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
        monkeypatch.setenv("SEAT_ENV_FILE", str(tmp_path / "no.env"))
        monkeypatch.setattr(seat.sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(seat, "_post_json", post)
        rc = seat.main(["--repo-root", str(tmp_path), *argv])
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


_TOOL_CALL = {"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}}


class TestPromptCaching:
    """Prompt-caching slice (2026-07-20 memo): a stable prompt_cache_key on EVERY POST (including
    the budget-exhausted final call), per-step usage telemetry, reasoning_content replay for xAI
    reasoning models, and a prefix-preserving budget-final payload (tools kept, tool_choice none)."""

    def test_chat_loop_key_on_every_post_and_usage_logged(self, monkeypatch, capsys, tmp_path):
        captured: list[dict] = []
        replies = [
            {
                "choices": [{"message": {"content": None, "tool_calls": [_TOOL_CALL]}}],
                "usage": {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 0}},
            },
            {
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 2600, "prompt_tokens_details": {"cached_tokens": 2048}},
            },
        ]

        def post(url, key, payload, timeout):
            captured.append(payload)
            return replies[len(captured) - 1]

        monkeypatch.setattr(seat, "_post_json", post)
        out = seat._run_agentic(
            "https://api.x.ai/v1", "grok-4.5", "k", "PROMPT", tmp_path,
            timeout=5, max_steps=5, max_bytes=1000,
        )
        assert out == "done"
        assert len(captured) == 2
        assert {p["prompt_cache_key"] for p in captured} == {seat._PROMPT_CACHE_KEY}
        err = capsys.readouterr().err
        assert "usage api=chat step=1 prompt_tokens=2000 cached_tokens=0" in err
        assert "usage api=chat step=2 prompt_tokens=2600 cached_tokens=2048" in err

    def test_chat_budget_final_keeps_tools_and_key_and_absent_usage_is_na(
        self, monkeypatch, capsys, tmp_path
    ):
        captured: list[dict] = []

        def post(url, key, payload, timeout):
            captured.append(payload)
            if len(captured) == 1:  # burn the single step on a tool call → forces the final POST
                return {"choices": [{"message": {"content": None, "tool_calls": [_TOOL_CALL]}}]}
            return {"choices": [{"message": {"content": "forced final"}}]}

        monkeypatch.setattr(seat, "_post_json", post)
        out = seat._run_agentic(
            "https://api.x.ai/v1", "grok-4.5", "k", "P", tmp_path,
            timeout=5, max_steps=1, max_bytes=1000,
        )
        assert out == "forced final"
        final = captured[-1]
        assert final["tools"] == captured[0]["tools"]  # prefix preserved: tools NOT dropped
        assert final["tool_choice"] == "none"
        assert all(p["prompt_cache_key"] == seat._PROMPT_CACHE_KEY for p in captured)
        # no usage object in the fake replies → n/a, never a fabricated 0
        assert "cached_tokens=n/a" in capsys.readouterr().err

    def test_responses_loop_and_budget_final_carry_key_and_tools(self, monkeypatch, capsys, tmp_path):
        captured: list[dict] = []

        def post(url, key, payload, timeout):
            captured.append(payload)
            if len(captured) == 1:
                return {
                    "output": [
                        {"type": "function_call", "call_id": "c1", "name": "list_dir", "arguments": "{}"}
                    ],
                    "usage": {"input_tokens": 1500, "input_tokens_details": {"cached_tokens": 0}},
                }
            return {
                "output_text": "final",
                "usage": {"input_tokens": 1900, "input_tokens_details": {"cached_tokens": 1408}},
            }

        monkeypatch.setattr(seat, "_post_json", post)
        out = seat._run_agentic_responses(
            "https://api.openai.com/v1", "gpt-5.6-luna", "k", "PROMPT", tmp_path,
            timeout=5, max_steps=1, max_bytes=1000,
        )
        assert out == "final"
        assert len(captured) == 2  # one step + the budget-exhausted final call
        final = captured[-1]
        assert final["tools"] == captured[0]["tools"] and final["tool_choice"] == "none"
        assert all(p["prompt_cache_key"] == seat._PROMPT_CACHE_KEY for p in captured)
        err = capsys.readouterr().err
        assert "usage api=responses step=1 prompt_tokens=1500 cached_tokens=0" in err
        assert "usage api=responses step=2 prompt_tokens=1900 cached_tokens=1408" in err

    def test_reasoning_content_replayed_when_present(self, monkeypatch, tmp_path):
        captured: list[dict] = []

        def post(url, key, payload, timeout):
            captured.append(payload)
            if len(captured) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "thinking...",
                                "tool_calls": [_TOOL_CALL],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(seat, "_post_json", post)
        out = seat._run_agentic(
            "https://api.x.ai/v1", "grok-4.5", "k", "P", tmp_path,
            timeout=5, max_steps=3, max_bytes=1000,
        )
        assert out == "ok"
        replayed = [m for m in captured[1]["messages"] if m.get("role") == "assistant"]
        assert replayed and replayed[0]["reasoning_content"] == "thinking..."

    def test_reasoning_content_absent_stays_absent(self, monkeypatch, tmp_path):
        captured: list[dict] = []

        def post(url, key, payload, timeout):
            captured.append(payload)
            if len(captured) == 1:
                return {"choices": [{"message": {"content": None, "tool_calls": [_TOOL_CALL]}}]}
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(seat, "_post_json", post)
        seat._run_agentic(
            "https://api.x.ai/v1", "grok-4.5", "k", "P", tmp_path,
            timeout=5, max_steps=3, max_bytes=1000,
        )
        replayed = [m for m in captured[1]["messages"] if m.get("role") == "assistant"]
        assert replayed and "reasoning_content" not in replayed[0]


class TestGatewayHeaders:
    """Provider-specific sticky headers do not cross the LiteLLM boundary."""

    def test_provider_specific_conv_id_is_omitted(self):
        h = seat._request_headers("https://api.x.ai/v1/chat/completions", "k")
        assert "x-grok-conv-id" not in h
        assert h["Authorization"] == "Bearer k"

    def test_non_xai_hosts_do_not(self):
        for url in (
            "https://api.openai.com/v1/responses",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "http://localhost:4000/v1/chat/completions",
            "https://fakex.ai/v1/chat/completions",  # hostname suffix lookalike, not *.x.ai
        ):
            assert "x-grok-conv-id" not in seat._request_headers(url, "k"), url

    def test_cache_write_tokens_in_stderr_line(self, monkeypatch, capsys, tmp_path):
        def post(url, key, payload, timeout):
            return {
                "choices": [{"message": {"content": "done"}}],
                "usage": {
                    "prompt_tokens": 3000,
                    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 2900},
                },
            }

        monkeypatch.setattr(seat, "_post_json", post)
        seat._run_agentic(
            "https://api.openai.com/v1", "gpt-5.5", "k", "P", tmp_path,
            timeout=5, max_steps=2, max_bytes=1000,
        )
        # a write-heavy miss (1.25x billed on gpt-5.6+) must be visible mid-run, not only in JSONL
        assert "cached_tokens=0 cache_write_tokens=2900" in capsys.readouterr().err


class TestUsageJsonl:
    def test_jsonl_written_when_env_set(self, monkeypatch, tmp_path):
        log = tmp_path / "usage.jsonl"
        monkeypatch.setenv("COLLAB_USAGE_LOG", str(log))
        captured: list[dict] = []
        replies = [
            {
                "choices": [{"message": {"content": None, "tool_calls": [_TOOL_CALL]}}],
                "usage": {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 0}},
            },
            {
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 2600, "prompt_tokens_details": {"cached_tokens": 2048}},
            },
        ]

        def post(url, key, payload, timeout):
            captured.append(payload)
            return replies[len(captured) - 1]

        monkeypatch.setattr(seat, "_post_json", post)
        seat._run_agentic(
            "https://api.x.ai/v1", "grok-4.5", "k", "P", tmp_path,
            timeout=5, max_steps=5, max_bytes=1000,
        )
        recs = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        assert [r["step"] for r in recs] == [1, 2]
        assert recs[0]["api"] == "chat" and recs[0]["adapter"] == "repo-seat"
        assert recs[1]["cached_tokens"] == 2048 and recs[1]["prompt_tokens"] == 2600
        assert all(r["prompt_cache_key"] == seat._PROMPT_CACHE_KEY for r in recs)

    def test_default_path_is_off_under_pytest(self, monkeypatch):
        # Hermetic guarantee: with the env unset, PYTEST_CURRENT_TEST keeps the default kit-root
        # JSONL OFF so no test can dirty collab/logs/ by forgetting the env var.
        monkeypatch.delenv("COLLAB_USAGE_LOG", raising=False)
        assert seat._usage_log_path() is None

    def test_disable_values(self, monkeypatch):
        for val in ("", "off", "OFF", "0", "false", "none"):
            monkeypatch.setenv("COLLAB_USAGE_LOG", val)
            assert seat._usage_log_path() is None, val


class TestCheckRootIsolation:
    """INT-037b: a --run-checks (read_test) seat runs against an ephemeral working-tree COPY so an
    allow-listed interpreter write cannot mutate the source it is judging (ADR-0006 enforceable check)."""

    def test_isolate_copies_tree_carries_edits_excludes_vcs_and_is_external(self, tmp_path):
        judged = tmp_path / "judged"
        (judged / "src").mkdir(parents=True)
        (judged / "src" / "m.py").write_text("x = 1  # uncommitted edit\n", encoding="utf-8")
        (judged / ".git").mkdir()
        (judged / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        copy, cleanup = seat._isolate_check_root(judged)
        try:
            assert copy.is_dir() and copy.resolve() != judged.resolve()
            # a COPY, not a git worktree: the uncommitted edit the judge must review is present
            assert (copy / "src" / "m.py").read_text(encoding="utf-8") == "x = 1  # uncommitted edit\n"
            assert not (copy / ".git").exists()  # VCS internals excluded (_SKIP_DIRS)
            # created OUTSIDE the judged tree — never nested under it
            assert judged.resolve() not in copy.resolve().parents and copy.resolve() != judged.resolve()
        finally:
            cleanup()
        assert not copy.exists()  # cleaned up

    def test_run_command_write_lands_in_copy_not_judged(self, tmp_path):
        judged = tmp_path / "judged"
        judged.mkdir()
        (judged / "keep.py").write_text("x = 1\n", encoding="utf-8")

        copy, cleanup = seat._isolate_check_root(judged)
        try:
            # an allow-listed interpreter the read_test seat can invoke tries to write; with cwd=copy it
            # lands in the throwaway, and the judged source is untouched.
            seat._tool_run_command(
                copy, "python -c \"open('evil.txt', 'w').write('x')\"", 100_000, 30.0
            )
            assert (copy / "evil.txt").exists()
            assert not (judged / "evil.txt").exists()  # judged source untouched — the INT-037b contract
        finally:
            cleanup()

    def test_keep_env_retains_copy_for_debug(self, tmp_path, monkeypatch):
        judged = tmp_path / "j"
        judged.mkdir()
        monkeypatch.setenv("COLLAB_KEEP_CHECK_ROOT", "1")
        copy, cleanup = seat._isolate_check_root(judged)
        cleanup()
        assert copy.exists()  # retained under COLLAB_KEEP_CHECK_ROOT=1
        shutil.rmtree(copy, ignore_errors=True)
