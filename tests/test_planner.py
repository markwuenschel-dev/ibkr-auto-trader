from datetime import UTC, datetime
from decimal import Decimal

from ibkr_trader.config import RiskPolicy
from ibkr_trader.domain.models import (
    ASSEMBLER_AUTHORITY,
    HoldingValuation,
    RiskContext,
    ValuationStatus,
)
from ibkr_trader.risk.control_state import RiskControlState
from ibkr_trader.risk.planner import RiskPlanner

NOW = datetime(2026, 1, 2, 15, tzinfo=UTC)


def make_context():
    return RiskContext._mint(
        ASSEMBLER_AUTHORITY,
        holdings={
            7: HoldingValuation(
                quantity=0,
                status=ValuationStatus.AVAILABLE,
                broker_market_value=Decimal(0),
                mark_available_at=NOW,
            )
        },
        net_liquidation=Decimal("10000"),
        buying_power=Decimal("5000"),
        maintenance_margin=Decimal("5000"),
        prices={7: Decimal("100")},
        price_basis={7: "LAST"},
        data_as_of={7: NOW},
        account_observed_at=NOW,
        as_of=NOW,
        context_digest="sealed-digest",
    )


def control():
    return RiskControlState(
        policy=RiskPolicy(version="reviewed-7"),
        session_date=NOW.date(),
        realized_daily_pnl=Decimal(0),
        observed_at=NOW,
    )


def test_planner_reduces_once_to_risk_limit_and_binds_evidence():
    plan = RiskPlanner().plan(
        {
            "symbol": "AAPL",
            "instrument_id": 7,
            "quantity": 100,
            "price": 100,
            "stop_price": 90,
            "lot_size": 10,
        },
        make_context(),
        control(),
    )
    assert plan.quantity == 10
    assert plan.planner_projection is not None
    assert plan.projection_is_authoritative is False
    assert plan.context_digest == "sealed-digest"
    assert plan.policy_version == "reviewed-7"
    assert plan.declined is False


def test_decline_is_a_valid_fixed_plan():
    plan = RiskPlanner().plan(
        {"symbol": "AAPL", "instrument_id": 7, "quantity": 1}, make_context(), control()
    )
    assert plan.declined is True
    assert plan.quantity == 0
    assert plan.planner_projection is None
    assert plan.decline_reason == "stop-loss-required"
