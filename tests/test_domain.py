"""PT-1 mint-seam regressions retained after RiskContext became assembler-minted."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibkr_trader.domain import (
    EXECUTION_AUTHORITY,
    RISK_AUTHORITY,
    ApprovedOrderIntent,
    ExecutableOrder,
    Side,
    StrategyIntent,
)

_NOW = datetime(2026, 7, 7, tzinfo=UTC)


def _approved() -> ApprovedOrderIntent:
    return ApprovedOrderIntent._mint(
        RISK_AUTHORITY,
        symbol="AAPL",
        side=Side.BUY,
        quantity=3,
        stop_price=Decimal("180.00"),
        approved_at=_NOW,
        ledger_ref="ledger:001",
    )


def test_strategy_intent_is_freely_constructible() -> None:
    assert StrategyIntent(symbol="AAPL", target_weight=Decimal("0.1")).symbol == "AAPL"


def test_guarded_orders_require_their_specific_authority() -> None:
    with pytest.raises(TypeError, match="designated issuer"):
        ApprovedOrderIntent(
            symbol="AAPL",
            side=Side.BUY,
            quantity=3,
            stop_price=Decimal("180"),
            approved_at=_NOW,
            ledger_ref="x",
        )
    with pytest.raises(TypeError):
        ApprovedOrderIntent._mint(
            EXECUTION_AUTHORITY,
            symbol="AAPL",
            side=Side.BUY,
            quantity=1,
            stop_price=Decimal("180"),
            approved_at=_NOW,
            ledger_ref="x",
        )
    approved = _approved()
    order = ExecutableOrder._mint(EXECUTION_AUTHORITY, approved=approved, mode="PAPER", idempotency_key="k1")
    assert order.approved is approved


def test_frozen_models_reject_mutation() -> None:
    with pytest.raises(ValidationError):
        StrategyIntent(symbol="AAPL", target_weight=Decimal("0.1")).symbol = "MSFT"  # type: ignore[misc]
