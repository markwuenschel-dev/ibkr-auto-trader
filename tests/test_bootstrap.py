"""PT-0 bootstrap tests — stdlib only (no ib_async/pydantic/numpy needed yet).

Pins the three things PT-0 must guarantee: the package imports, the control plane is PAPER-by-default
and live-refusing, and the §8 telemetry envelope emits a well-formed, hashed, replayable event.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from ibkr_trader import __version__, app
from ibkr_trader.config import Mode, RiskPolicy, Settings, resolve_mode, submission_allowed
from ibkr_trader.pack import TRADING_PACK
from ibkr_trader.telemetry import SCHEMA_VERSION, Emitter


def test_package_and_subpackages_import():
    for sub in ("domain", "ibkr", "strategy", "risk", "execution", "state", "audit"):
        __import__(f"ibkr_trader.{sub}")
    assert __version__ == "0.0.0"


class TestControlPlaneIsPaperFirst:
    def test_default_mode_is_paper(self):
        assert Settings().effective_mode() is Mode.PAPER

    def test_unset_request_resolves_to_paper(self):
        assert resolve_mode(None) is Mode.PAPER

    def test_live_refused_without_enable(self):
        assert resolve_mode(Mode.LIVE, live_enabled=False) is Mode.PAPER
        assert resolve_mode(Mode.LIVE, live_enabled=True) is Mode.LIVE

    def test_paper_only_master_guard_blocks_live(self):
        # Even a LIVE request with live_enabled is refused while paper_only is set.
        s = Settings(mode=Mode.LIVE, live_enabled=True, paper_only=True)
        assert s.effective_mode() is Mode.PAPER

    def test_kill_and_pause_block_submission(self):
        assert submission_allowed(Mode.PAPER) is True
        assert submission_allowed(Mode.KILL_SWITCHED) is False
        assert submission_allowed(Mode.PAUSED) is False

    def test_default_risk_policy_matches_protocol(self):
        policy = RiskPolicy()
        assert policy.max_risk_per_trade == Decimal("0.01")
        assert policy.daily_realized_lockout_pct == Decimal("0.03")
        assert policy.leverage_cap == Decimal("1.5") and policy.stop_loss_required is True


_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "trace_id",
        "span_id",
        "parent_span_id",
        "run_id",
        "task_id",
        "agent_role",
        "stage",
        "decision",
        "metrics",
        "gates",
        "eval",
        "risk",
        "failure",
        "event_id",
    }
)


class TestTelemetryEnvelope:
    def test_emit_writes_valid_jsonl_envelope(self, tmp_path):
        em = Emitter(log_path=tmp_path / "t.jsonl")
        ev = em.emit(
            stage="unit.test",
            agent_role="builder",
            decision={"action": "accept", "reason_codes": ["x"], "confidence": None},
        )
        assert ev.keys() >= _REQUIRED_KEYS
        assert ev["schema_version"] == SCHEMA_VERSION and ev["risk"] is None  # fail-closed: risk null now
        lines = (tmp_path / "t.jsonl").read_text("utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["event_id"] == ev["event_id"]

    def test_invalid_decision_action_rejected(self, tmp_path):
        em = Emitter(log_path=tmp_path / "t.jsonl")
        with pytest.raises(ValueError):
            em.emit(stage="x", decision={"action": "bogus"})

    def test_append_only(self, tmp_path):
        em = Emitter(log_path=tmp_path / "t.jsonl")
        em.emit(stage="a")
        em.emit(stage="b")
        assert len((tmp_path / "t.jsonl").read_text("utf-8").splitlines()) == 2


class TestPackDeclaration:
    def test_trading_pack_is_execution_oracle(self):
        # §5.7: an execution oracle -> auto-block authority + true-correctness R_fw bound.
        assert TRADING_PACK.oracle == "execution"
        assert "pytest" in TRADING_PACK.executables

    def test_pack_carries_protocol_limits(self):
        t = TRADING_PACK.acceptance_thresholds
        assert t["max_risk_per_trade"] == 0.01 and t["live_rejected_by_default"] is True


def test_app_bootstrap_emits_event(tmp_path):
    ev = app.bootstrap(emitter=Emitter(log_path=tmp_path / "run.jsonl"))
    assert ev["stage"] == "app.bootstrap" and ev["task_id"] == "PT-0"
