"""RiskControlState contract tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibkr_trader.config import RiskPolicy
from ibkr_trader.risk.control_state import RiskControlState


def test_control_state_carries_only_the_control_plane_facts_and_is_frozen():
    observed_at = datetime(2026, 7, 11, 15, 30, tzinfo=UTC)
    state = RiskControlState(
        policy=RiskPolicy(version="reviewed-2026-07-11"),
        session_date=date(2026, 7, 11),
        realized_daily_pnl=Decimal("-12.34"),
        observed_at=observed_at,
    )

    assert state.policy.version == "reviewed-2026-07-11"
    assert state.realized_daily_pnl == Decimal("-12.34")
    assert isinstance(state.realized_daily_pnl, Decimal)
    assert state.observed_at == observed_at
    assert set(RiskControlState.model_fields) == {
        "policy",
        "session_date",
        "realized_daily_pnl",
        "observed_at",
    }
    assert "mode" not in RiskControlState.model_fields
    assert "reservation" not in RiskControlState.model_fields
    assert "idempotency_key" not in RiskControlState.model_fields

    with pytest.raises(ValidationError):
        state.realized_daily_pnl = Decimal("0")  # type: ignore[misc]


def test_control_state_forbids_mode_and_reservation_inputs():
    fields = {
        "policy": RiskPolicy(),
        "session_date": date(2026, 7, 11),
        "realized_daily_pnl": Decimal("0"),
        "observed_at": datetime(2026, 7, 11, tzinfo=UTC),
    }

    with pytest.raises(ValidationError):
        RiskControlState(**fields, mode="PAPER")
    with pytest.raises(ValidationError):
        RiskControlState(**fields, reservation="claimed")
    with pytest.raises(ValidationError):
        RiskControlState(**fields, idempotency_key="order-1")
