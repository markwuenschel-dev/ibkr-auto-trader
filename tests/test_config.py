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


def test_default_risk_policy_is_versioned_decimal_and_frozen() -> None:
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


def test_threshold_boundaries_use_decimal_and_drawdown_is_independent() -> None:
    policy = RiskPolicy()
    session_start_equity = Decimal("1000.00")

    realized_lockout_boundary = -policy.daily_realized_lockout_pct * session_start_equity
    drawdown_boundary = -policy.session_drawdown_pct * session_start_equity

    assert realized_lockout_boundary == Decimal("-30.0000")
    assert drawdown_boundary == Decimal("-100.0000")
    assert policy.session_drawdown_pct != policy.daily_realized_lockout_pct


def test_settings_uses_a_fresh_default_risk_policy() -> None:
    first = Settings()
    second = Settings()

    assert isinstance(first.risk, RiskPolicy)
    assert first.risk is not second.risk


# --------------------------------------------------------------------------- #
# float ingress — ADR-0003: limits convert float -> Decimal BEFORE they
# participate in any money calculation; "no float in money arithmetic".
# Decimal annotations alone did not enforce this: lax mode coerced a float via
# str(), preserving binary imprecision inside a reviewed money limit.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", _DECIMAL_LIMIT_FIELDS)
def test_a_float_limit_is_rejected_not_coerced(field: str) -> None:
    with pytest.raises(ValidationError):
        RiskPolicy(**{field: 0.1})  # type: ignore[arg-type]  # the float is the point


def test_an_imprecise_float_cannot_enter_a_limit() -> None:
    """The reproduction: 0.1 + 0.2 is not 0.3, and str()-coercion preserved that exactly."""
    with pytest.raises(ValidationError):
        RiskPolicy(session_drawdown_pct=0.1 + 0.2)  # type: ignore[arg-type]


def test_control_2_fires_at_exactly_the_drawdown_boundary() -> None:
    """A session exactly 30% down must trip a 30% limit.

    With float ingress, session_drawdown_pct held Decimal("0.30000000000000004"); the boundary
    landed just past -300.00 and the loss control did not fire.
    """
    policy = RiskPolicy(session_drawdown_pct=Decimal("0.30"))
    realized, session_start_equity = Decimal("-300.00"), Decimal("1000.00")

    assert realized <= -policy.session_drawdown_pct * session_start_equity


def test_stop_loss_required_rejects_a_falsy_int() -> None:
    """The mandatory-stop flag must not be silently disabled by 0."""
    with pytest.raises(ValidationError):
        RiskPolicy(stop_loss_required=0)  # type: ignore[arg-type]


def test_stop_loss_required_rejects_a_truthy_int() -> None:
    with pytest.raises(ValidationError):
        RiskPolicy(stop_loss_required=1)  # type: ignore[arg-type]


def test_decimal_limits_are_still_accepted() -> None:
    policy = RiskPolicy(session_drawdown_pct=Decimal("0.30"), stop_loss_required=False)

    assert policy.session_drawdown_pct == Decimal("0.30")
    assert policy.stop_loss_required is False
