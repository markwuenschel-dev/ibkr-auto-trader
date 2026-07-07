"""PT-1 domain tests — the value objects and, above all, the constructibility seam.

The load-bearing guarantee: a strategy can construct a StrategyIntent but CANNOT construct (mint) an
ApprovedOrderIntent or ExecutableOrder — only Risk & Sizing / Execution Control can, each with its own
authority. These tests pin that, plus immutability.
"""

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
    RiskContext,
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


class TestFreelyConstructible:
    def test_strategy_intent_is_freely_constructible(self):
        si = StrategyIntent(symbol="AAPL", target_weight=Decimal("0.1"), rationale="drift")
        assert si.symbol == "AAPL"

    def test_risk_context_is_freely_constructible(self):
        rc = RiskContext(
            as_of=_NOW,
            net_liquidation=Decimal("2000"),
            buying_power=Decimal("4000"),
            maintenance_margin=Decimal("0"),
        )
        assert rc.net_liquidation == Decimal("2000")


class TestConstructibilitySeam:
    def test_approved_order_intent_cannot_be_constructed_directly(self):
        with pytest.raises(TypeError, match="minted only"):
            ApprovedOrderIntent(
                symbol="AAPL",
                side=Side.BUY,
                quantity=3,
                stop_price=Decimal("180"),
                approved_at=_NOW,
                ledger_ref="x",
            )

    def test_executable_order_cannot_be_constructed_directly(self):
        with pytest.raises(TypeError, match="minted only"):
            ExecutableOrder(approved=_approved(), mode="PAPER", idempotency_key="k1")

    def test_wrong_authority_cannot_mint(self):
        # Execution's authority cannot mint an ApprovedOrderIntent, and vice-versa.
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
        with pytest.raises(TypeError):
            ExecutableOrder._mint(RISK_AUTHORITY, approved=_approved(), mode="PAPER", idempotency_key="k")

    def test_correct_authority_mints(self):
        approved = _approved()
        assert approved.symbol == "AAPL" and approved.quantity == 3
        order = ExecutableOrder._mint(
            EXECUTION_AUTHORITY, approved=approved, mode="PAPER", idempotency_key="k1"
        )
        assert order.approved is approved and order.mode == "PAPER"


class TestImmutability:
    def test_intent_is_frozen(self):
        si = StrategyIntent(symbol="AAPL", target_weight=Decimal("0.1"))
        with pytest.raises(ValidationError):
            si.symbol = "MSFT"  # type: ignore[misc]

    def test_minted_order_is_frozen(self):
        order = ExecutableOrder._mint(
            EXECUTION_AUTHORITY, approved=_approved(), mode="PAPER", idempotency_key="k1"
        )
        with pytest.raises(ValidationError):
            order.mode = "LIVE"  # type: ignore[misc]
