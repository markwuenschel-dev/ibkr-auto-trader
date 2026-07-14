"""RiskPolicy control-plane contract tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibkr_trader.config import RiskPolicy, Settings

_DECIMAL_LIMIT_FIELDS = (
    "max_risk_per_trade",
    "daily_realized_lockout_pct",
    "session_drawdown_pct",
    "leverage_cap",
)


def test_default_risk_policy_is_versioned_decimal_and_frozen():
    policy = RiskPolicy()

    assert policy.version == "v1"
    assert policy.max_risk_per_trade == Decimal("0.01")
    assert policy.daily_realized_lockout_pct == Decimal("0.03")
    assert policy.session_drawdown_pct == Decimal("0.10")
    assert policy.leverage_cap == Decimal("1.5")
    assert policy.stop_loss_required is True
    assert all(isinstance(getattr(policy, field), Decimal) for field in _DECIMAL_LIMIT_FIELDS)
    with pytest.raises(ValidationError):
        policy.leverage_cap = Decimal("2")  # type: ignore[misc]


def test_threshold_boundaries_are_decimal_and_drawdown_is_independent():
    policy = RiskPolicy()
    session_start_equity = Decimal("1000.00")

    realized_lockout_boundary = -policy.daily_realized_lockout_pct * session_start_equity
    drawdown_boundary = -policy.session_drawdown_pct * session_start_equity

    assert realized_lockout_boundary == Decimal("-30.0000")
    assert drawdown_boundary == Decimal("-100.0000")
    assert policy.session_drawdown_pct != policy.daily_realized_lockout_pct


def test_settings_uses_a_fresh_default_risk_policy():
    first = Settings()
    second = Settings()

    assert isinstance(first.risk, RiskPolicy)
    assert first.risk is not second.risk
