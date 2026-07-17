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


def make_context(*, generation=7):
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
        generation=generation,  # the session generation the assembler fenced + sealed
        context_digest="sealed-digest",
    )


def control(*, session_generation=7):
    return RiskControlState(
        policy=RiskPolicy(version="reviewed-7"),
        session_date=NOW.date(),
        realized_daily_pnl=Decimal(0),
        observed_at=NOW,
        session_generation=session_generation,
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
    assert plan.decision_generation == 7  # bound from the SEALED context, not a default
    assert plan.session_generation == 7  # ...and from the control state
    assert plan.declined is False


def test_generation_binds_each_field_to_its_own_source():
    """ADR-0003: a plan binds decision AND session generation. The 2026-07-16 regression.

    Both fields were `Any = None` and the planner read them off attributes that existed on neither
    RiskContext nor RiskControlState, so every plan bound None while the diff looked correct. pyright
    could not see it (None is a valid `Any`), no test asserted it, no lane raised it, and the reviewer
    marked it `[met]` citing the real assignment line.

    The two sources are given DISTINCT values on purpose: an implementation that binds one number to
    both fields — or reads the wrong object — passes an equal-values test and fails this one.
    """
    ctx = make_context(generation=41)  # the session generation the context was SEALED at
    ctl = control(session_generation=42)  # ...and the one observed when the control state was built
    plan = RiskPlanner().plan(
        {"symbol": "AAPL", "instrument_id": 7, "quantity": 10, "price": 100, "stop_price": 90},
        ctx,
        ctl,
    )
    assert plan.decision_generation == 41
    assert plan.session_generation == 42
    # A difference is meaningful, not noise: the session reconnected between the decision seal and
    # planning, so the plan mixes pre- and post-reconnect state. The plan binds it truthfully and the
    # approver re-checks; what must never happen is both silently reading None.
    assert plan.decision_generation != plan.session_generation


def test_a_declined_plan_still_binds_its_generations():
    # A decline is a fixed plan, not an absence of one — it carries the same provenance, or an
    # auditor cannot tell which session refused.
    plan = RiskPlanner().plan(
        {"symbol": "AAPL", "instrument_id": 7, "quantity": 1},
        make_context(generation=41),
        control(session_generation=42),
    )
    assert plan.declined is True
    assert plan.decision_generation == 41 and plan.session_generation == 42


def test_decline_is_a_valid_fixed_plan():
    plan = RiskPlanner().plan(
        {"symbol": "AAPL", "instrument_id": 7, "quantity": 1}, make_context(), control()
    )
    assert plan.declined is True
    assert plan.quantity == 0
    assert plan.planner_projection is None
    assert plan.decline_reason == "stop-loss-required"
