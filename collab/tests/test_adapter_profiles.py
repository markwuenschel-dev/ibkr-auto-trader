"""Tests for adapter_profiles — typed adapters + enforced seat policies (ADR-0003 D3, Phase 2).

Pins the capability contract that structurally kills the 030 argv crash and the wrongly-editing
assessment seats: an adapter renders only flags it understands, a managed seat's policy must be
enforceable or the seat does not compile, and a legacy model_args tail carrying a foreign flag is
refused up front.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import adapter_profiles as ap  # noqa: E402
import collab_common as cc  # noqa: E402

_REPO_SEAT = "C:\\repo\\collab\\tools\\adapters\\openai-repo-seat.py"
_COMPAT_SEAT = "C:\\repo\\collab\\tools\\adapters\\openai-compatible-seat.py"


def _models() -> dict:
    return {
        "claude-m": {"cmd": ["claude", "-p", "--model", "claude-opus-4-8"],
                     "unset_env": ["ANTHROPIC_API_KEY"]},
        "openai-repo-m": {"cmd": ["python", _REPO_SEAT, "--base", "https://api.openai.com/v1",
                                  "--model", "gpt-5.6", "--key-env", "OPENAI_API_KEY",
                                  "--repo-root", "C:\\repo"]},
        "openai-repo-build-m": {"cmd": ["python", _REPO_SEAT, "--base", "https://api.openai.com/v1",
                                        "--model", "gpt-5.6", "--key-env", "OPENAI_API_KEY",
                                        "--repo-root", "C:\\repo", "--write"]},
        "openai-compat-m": {"cmd": ["python", _COMPAT_SEAT, "--base", "https://api.openai.com/v1",
                                    "--model", "gpt-5.6", "--key-env", "OPENAI_API_KEY",
                                    "--api", "auto"]},
    }


class TestThe030Bug:
    def test_openai_rejects_claude_permission_model_args(self):
        # The exact live failure: Claude-only flags in model_args on an OpenAI-adapter model.
        cfg = {"backend": "cli", "model": "openai-repo-m",
               "model_args": ["--permission-mode", "acceptEdits",
                              "--allowedTools", "Bash(uv run pytest:*)"]}
        with pytest.raises(cc.CollabError, match="Claude-only"):
            ap.compile_seat("breaker", cfg, _models())

    def test_no_permission_mode_can_reach_an_openai_seat(self):
        # A managed OpenAI read_test seat emits --run-checks, never --permission-mode/--allowedTools.
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "breaker", "access": "read_test"}
        out = ap.compile_seat("breaker", cfg, _models())
        assert "--permission-mode" not in out["cmd"]
        assert "--allowedTools" not in out["cmd"]


class TestReadTestEnforcement:
    def test_openai_read_test_rejected_without_run_checks(self):
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "verifier", "access": "read_test"}
        with pytest.raises(cc.CollabError, match="run-checks"):
            ap.compile_seat("verifier", cfg, _models(), run_checks_supported=False)

    def test_openai_read_test_accepted_with_run_checks(self):
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "verifier", "access": "read_test"}
        out = ap.compile_seat("verifier", cfg, _models(), run_checks_supported=True)
        assert "--run-checks" in out["cmd"]
        assert "--write" not in out["cmd"]  # never granted write for an assessment seat

    def test_claude_read_test_is_non_editing(self):
        cfg = {"backend": "cli", "model": "claude-m", "role": "breaker", "access": "read_test"}
        out = ap.compile_seat("breaker", cfg, _models())
        assert "acceptEdits" not in out["cmd"]  # no auto-accept of edits
        assert "--allowedTools" in out["cmd"]
        assert "Bash(uv run pytest:*)" in out["cmd"]


class TestAdapterAppropriateFlags:
    def test_claude_write_accepts_edits(self):
        cfg = {"backend": "cli", "model": "claude-m", "role": "builder", "access": "write"}
        out = ap.compile_seat("builder", cfg, _models())
        assert out["cmd"][:4] == ["claude", "-p", "--model", "claude-opus-4-8"]
        assert "acceptEdits" in out["cmd"]

    def test_claude_read_is_plan_mode(self):
        cfg = {"backend": "cli", "model": "claude-m", "role": "reviewer", "access": "read"}
        out = ap.compile_seat("reviewer", cfg, _models())
        assert "plan" in out["cmd"]
        assert "acceptEdits" not in out["cmd"]

    def test_openai_write_emits_write_flag_only(self):
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "builder", "access": "write"}
        out = ap.compile_seat("builder", cfg, _models())
        assert "--write" in out["cmd"]
        assert not any(f in out["cmd"] for f in ap._CLAUDE_ONLY_FLAGS)

    def test_openai_read_has_no_capability_flag(self):
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "reviewer", "access": "read"}
        out = ap.compile_seat("reviewer", cfg, _models())
        assert "--write" not in out["cmd"]
        assert "--run-checks" not in out["cmd"]
        assert "--repo-root" in out["cmd"]  # config kept

    def test_base_argv_strips_baked_write(self):
        # A -build template bakes --write; a read policy over it must NOT inherit write.
        cfg = {"backend": "cli", "model": "openai-repo-build-m", "role": "reviewer", "access": "read"}
        out = ap.compile_seat("reviewer", cfg, _models())
        assert "--write" not in out["cmd"]


class TestTextOnlyAdapter:
    def test_read_ok_write_rejected(self):
        read_cfg = {"backend": "cli", "model": "openai-compat-m", "role": "reviewer", "access": "read"}
        assert ap.compile_seat("reviewer", read_cfg, _models())["adapter"] == ap.OPENAI_COMPAT
        write_cfg = {"backend": "cli", "model": "openai-compat-m", "role": "builder", "access": "write"}
        with pytest.raises(cc.CollabError, match="no repo access"):
            ap.compile_seat("builder", write_cfg, _models())


class TestLegacyAndErrors:
    def test_explicit_cmd_seat_not_switchable(self):
        cfg = {"backend": "cli", "cmd": ["claude", "-p"]}
        out = ap.compile_seat("human", cfg, {})
        assert out["switchable"] is False
        assert out["adapter"] == ap.LEGACY
        assert out["cmd"] == ["claude", "-p"]

    def test_absent_model_raises(self):
        cfg = {"backend": "cli", "model": "nope", "access": "read"}
        with pytest.raises(cc.CollabError, match="absent from the 'models' catalog"):
            ap.compile_seat("reviewer", cfg, _models())

    def test_managed_seats_are_switchable(self):
        cfg = {"backend": "cli", "model": "openai-repo-m", "role": "reviewer", "access": "read"}
        assert ap.compile_seat("reviewer", cfg, _models())["switchable"] is True


class TestCompatibilityGateIsSingle:
    """The same compile_seat gate is used at dashboard-save time AND at run start — one function,
    one verdict, so an incompatible seat can never be persisted OR claim work."""

    def test_bad_seat_always_raises_good_seat_always_compiles(self):
        models = _models()
        bad = {"backend": "cli", "model": "openai-repo-m",
               "model_args": ["--permission-mode", "acceptEdits"]}
        good = {"backend": "cli", "model": "openai-repo-m", "role": "builder", "access": "write"}
        for _ in range(2):  # both call sites exercise the identical function
            with pytest.raises(cc.CollabError):
                ap.compile_seat("breaker", bad, models)
            assert ap.compile_seat("builder", good, models)["cmd"]


class TestFingerprint:
    def test_stable_and_sensitive(self):
        models = _models()
        read = ap.compile_seat("reviewer", {"model": "openai-repo-m", "access": "read"}, models)
        write = ap.compile_seat("builder", {"model": "openai-repo-m", "access": "write"}, models)
        again = ap.compile_seat("reviewer", {"model": "openai-repo-m", "access": "read"}, models)
        assert ap.seat_profile_fingerprint(read) == ap.seat_profile_fingerprint(again)
        assert ap.seat_profile_fingerprint(read) != ap.seat_profile_fingerprint(write)


class TestAudit:
    def test_seat_change_recorded(self, tmp_path):
        p = ap.audit_seat_change(
            str(tmp_path), "reviewer",
            {"model": "grok-4.5"}, {"model": "gpt-5.6"},
            by="dashboard-web", ts="2026-07-13T00:00:00Z",
        )
        lines = [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]
        assert len(lines) == 1
        assert lines[0]["seat"] == "reviewer"
        assert lines[0]["by"] == "dashboard-web"
        assert lines[0]["new"] == {"model": "gpt-5.6"}

    def test_appends(self, tmp_path):
        for i in range(3):
            ap.audit_seat_change(str(tmp_path), "breaker", None, {"model": f"m{i}"},
                                 by="test", ts=f"2026-07-13T00:00:0{i}Z")
        p = tmp_path / "autopilot" / "seats" / "audit.jsonl"
        assert len([x for x in p.read_text("utf-8").splitlines() if x.strip()]) == 3
