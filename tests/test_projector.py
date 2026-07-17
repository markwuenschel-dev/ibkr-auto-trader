from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ibkr_trader.config import RiskPolicy
from ibkr_trader.domain.models import (
    ASSEMBLER_AUTHORITY,
    HoldingValuation,
    RiskContext,
    Side,
    ValuationStatus,
)
from ibkr_trader.risk.control_state import RiskControlState
from ibkr_trader.risk.projector import PortfolioProjector, UnpricedHoldingError

NOW = datetime(2026, 1, 2, 15, tzinfo=UTC)


def context(unavailable=False):
    holding = HoldingValuation(
        quantity=10,
        status=ValuationStatus.UNAVAILABLE if unavailable else ValuationStatus.AVAILABLE,
        broker_market_value=None if unavailable else Decimal("1000"),
        mark_available_at=None if unavailable else NOW,
    )
    return RiskContext._mint(
        ASSEMBLER_AUTHORITY,
        holdings={7: holding},
        net_liquidation=Decimal("10000"),
        buying_power=Decimal("5000"),
        maintenance_margin=Decimal("3000"),
        prices={7: Decimal("100")},
        price_basis={7: "LAST"},
        data_as_of={7: NOW},
        account_observed_at=NOW,
        as_of=NOW,
        generation=7,  # the session generation the assembler fenced + sealed
        context_digest="ctx",
    )


def control():
    return RiskControlState(
        policy=RiskPolicy(),
        session_date=NOW.date(),
        realized_daily_pnl=Decimal(0),
        observed_at=NOW,
        session_generation=7,
    )


def test_incremental_debit_and_broker_market_value_are_used():
    result = PortfolioProjector().project(
        {
            "instrument_id": 7,
            "quantity": 5,
            "side": Side.BUY,
            "price": Decimal("100"),
            "stop_price": Decimal("90"),
        },
        context(),
        control(),
    )
    assert result.notional == Decimal("500")
    assert result.incremental_buying_power_debit == Decimal("500")
    assert result.resulting_gross_leverage == Decimal("0.15")
    assert result.max_loss_if_stopped == Decimal("50")


def test_unavailable_holding_fails_closed():
    with pytest.raises(UnpricedHoldingError):
        PortfolioProjector().project(
            {"instrument_id": 7, "quantity": 1, "price": 100}, context(True), control()
        )


def test_stop_direction_and_price_are_validated():
    with pytest.raises(ValueError):
        PortfolioProjector().project(
            {
                "instrument_id": 7,
                "quantity": 1,
                "side": Side.BUY,
                "price": 100,
                "stop_price": 110,
            },
            context(),
            control(),
        )
    with pytest.raises(ValueError):
        PortfolioProjector().project({"instrument_id": 7, "quantity": 1, "price": 0}, context(), control())
