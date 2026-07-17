"""Contract tests for the single REDUCE_ONLY predicate and mint seam."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ibkr_trader.config import Mode, submission_allowed
from ibkr_trader.domain import RISK_AUTHORITY, ApprovedOrderIntent, Side
from ibkr_trader.risk.reduce_only import ReduceOnlyLatch, ReduceOnlyViolation, is_reducing

_SESSION = date(2026, 7, 11)
_NOW = datetime(2026, 7, 11, tzinfo=UTC)


@given(current=st.integers(), resulting=st.integers())
def test_is_reducing_matches_signed_position_contract(current: int, resulting: int) -> None:
    expected = abs(resulting) < abs(current) and not (
        (current > 0 and resulting < 0) or (current < 0 and resulting > 0)
    )
    assert is_reducing(current, resulting) is expected


@pytest.mark.parametrize(
    ("current", "resulting", "expected"),
    [
        (100, 50, True),
        (-100, -50, True),
        (100, 0, True),
        (-100, 0, True),
        (0, 100, False),
        (100, 150, False),
        (-100, -150, False),
        (100, -100, False),
        (-100, 100, False),
    ],
)
def test_is_reducing_examples(current: int, resulting: int, expected: bool) -> None:
    assert is_reducing(current, resulting) is expected


def _mint(*, latch: ReduceOnlyLatch, current_qty: int, side: Side, quantity: int) -> ApprovedOrderIntent:
    return ApprovedOrderIntent._mint(
        RISK_AUTHORITY,
        symbol="AAPL",
        side=side,
        quantity=quantity,
        stop_price=Decimal("180"),
        approved_at=_NOW,
        ledger_ref="ledger:reduce-only",
        reduce_only_latch=latch,
        session_date=_SESSION,
        current_qty=current_qty,
    )


def test_latched_session_allows_reductions_and_full_exits_only() -> None:
    latch = ReduceOnlyLatch()
    latch.set(_SESSION)
    assert _mint(latch=latch, current_qty=100, side=Side.SELL, quantity=40).quantity == 40
    assert _mint(latch=latch, current_qty=-100, side=Side.BUY, quantity=100).quantity == 100
    with pytest.raises(ReduceOnlyViolation):
        _mint(latch=latch, current_qty=100, side=Side.BUY, quantity=1)
    with pytest.raises(ReduceOnlyViolation):
        _mint(latch=latch, current_qty=100, side=Side.SELL, quantity=200)


@pytest.mark.parametrize(("current_qty", "side"), [(100, Side.BUY), (-100, Side.SELL)])
def test_latched_session_rejects_invalid_quantities(current_qty: int, side: Side) -> None:
    latch = ReduceOnlyLatch()
    latch.set(_SESSION)
    with pytest.raises(ValueError, match="quantity must be a positive int"):
        _mint(latch=latch, current_qty=current_qty, side=side, quantity=0)
    with pytest.raises(ValueError, match="quantity must be a positive int"):
        _mint(latch=latch, current_qty=current_qty, side=side, quantity=-40)


def test_clearing_restores_minting_without_auto_flatten() -> None:
    latch = ReduceOnlyLatch()
    latch.latch(_SESSION)
    latch.clear(_SESSION)
    assert not latch.is_latched(_SESSION)
    assert _mint(latch=latch, current_qty=0, side=Side.BUY, quantity=10).quantity == 10


def test_mint_fails_closed_without_position_context() -> None:
    with pytest.raises(ValueError, match="session_date and current_qty"):
        ApprovedOrderIntent._mint(
            RISK_AUTHORITY,
            symbol="AAPL",
            side=Side.BUY,
            quantity=1,
            stop_price=Decimal("180"),
            approved_at=_NOW,
            ledger_ref="missing",
            reduce_only_latch=ReduceOnlyLatch(),
        )


def test_reduce_only_is_not_a_hard_submission_halt() -> None:
    assert submission_allowed(Mode.REDUCE_ONLY)
