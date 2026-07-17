"""PT-4c assembler tests — the sole ``RiskContext`` minter, all via fakes (ADR-0002 ④/⑤, ADR-0003 D4).

Proves: a broker read yields a sealed, conId-keyed ``RiskContext``; the mint guard blocks direct
construction; held valuation seeds from the account broker mark; the causal gate withholds look-ahead and
leaves genuinely unpriced instruments absent (never a fabricated zero); and a reconnect mid-capture is
fenced off, never spliced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ibkr_trader.decision import DecisionContextAssembler, GenerationFenceError
from ibkr_trader.domain import RiskContext, ValuationStatus
from ibkr_trader.ibkr import (
    FakeAccountGateway,
    FakeMarketDataFeed,
    FakeSession,
    FixedClock,
    HeldPosition,
    QuoteField,
)
from ibkr_trader.ibkr.marketdata import BASIS_BROKER_MARK, BASIS_LAST

_SEAL = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
AAPL, MSFT, GOOG = 265598, 272093, 208813720


@dataclass
class FixedDecisionClock:
    """Seals ``decision_at`` at a fixed instant (injected, never caller-supplied)."""

    moment: datetime

    def now(self) -> datetime:
        return self.moment


def _summary() -> dict[str, str]:
    return {"NetLiquidation": "2000.50", "BuyingPower": "8000.00", "MaintMarginReq": "150.25"}


def _held(con_id, symbol, qty, *, mv=None, mark=None, mark_at=None) -> HeldPosition:
    return HeldPosition.from_broker(
        instrument_id=con_id,
        symbol=symbol,
        quantity=qty,
        market_value=Decimal(mv) if mv is not None else None,
        market_price=Decimal(mark) if mark is not None else None,
        mark_available_at=mark_at,
    )


def _qf(value, *, at, basis):
    return QuoteField(value=Decimal(value), available_at=at, basis=basis)


def _assembler(*, held, quotes, seal=_SEAL, session=None):
    session = session or FakeSession(generation=0)
    gateway = FakeAccountGateway(clock=FixedClock(seal), summary=_summary(), held=held, session=session)
    feed = FakeMarketDataFeed(quotes=quotes, session=session)
    asyncio.run(gateway.connect())
    assembler = DecisionContextAssembler(gateway, feed, decision_time_source=FixedDecisionClock(seal))
    return assembler, session


class TestMintAndShape:
    def test_capture_mints_sealed_conid_keyed_context(self):
        assembler, _ = _assembler(
            held=[
                _held(AAPL, "AAPL", 10, mv="1500", mark="150", mark_at=_SEAL - timedelta(seconds=1)),
                _held(MSFT, "MSFT", -4),  # UNAVAILABLE
            ],
            quotes={MSFT: [_qf("250", at=_SEAL - timedelta(seconds=1), basis=BASIS_LAST)]},
        )
        ctx = asyncio.run(assembler.capture([MSFT]))
        assert isinstance(ctx, RiskContext)
        assert ctx.as_of == _SEAL
        assert all(isinstance(k, int) for k in ctx.holdings)
        # positions derived from holdings, conId-keyed.
        assert ctx.positions == {AAPL: 10, MSFT: -4}
        # AAPL priced from the account BROKER_MARK; MSFT priced from the feed LAST.
        assert ctx.prices[AAPL] == Decimal("150")
        assert ctx.price_basis[AAPL] == BASIS_BROKER_MARK
        assert ctx.prices[MSFT] == Decimal("250")
        assert ctx.price_basis[MSFT] == BASIS_LAST
        assert ctx.context_digest.startswith("context:")

    def test_direct_construction_is_blocked_by_mint_guard(self):
        with pytest.raises(TypeError):
            RiskContext(holdings={}, net_liquidation=Decimal("1"))  # type: ignore[call-arg]  # no authority

    def test_requested_non_held_instrument_is_priced_but_not_a_holding(self):
        assembler, _ = _assembler(
            held=[_held(AAPL, "AAPL", 10, mv="1500", mark="150", mark_at=_SEAL - timedelta(seconds=1))],
            quotes={GOOG: [_qf("2800", at=_SEAL - timedelta(seconds=1), basis=BASIS_LAST)]},
        )
        ctx = asyncio.run(assembler.capture([GOOG]))
        assert GOOG in ctx.prices and ctx.prices[GOOG] == Decimal("2800")
        assert GOOG not in ctx.holdings  # requested for execution, not held
        assert ctx.positions == {AAPL: 10}


class TestValuationFailClosed:
    def test_unavailable_holding_without_causal_price_is_absent_never_zero(self):
        assembler, _ = _assembler(
            held=[_held(MSFT, "MSFT", -4)],  # UNAVAILABLE, no account mark
            quotes={},  # feed serves nothing for it
        )
        ctx = asyncio.run(assembler.capture([]))
        assert ctx.holdings[MSFT].status is ValuationStatus.UNAVAILABLE
        assert ctx.holdings[MSFT].broker_market_value is None
        assert MSFT not in ctx.prices  # absent, never a fabricated 0

    def test_future_only_availability_is_withheld_even_in_live(self):
        assembler, _ = _assembler(
            held=[_held(MSFT, "MSFT", -4)],
            quotes={MSFT: [_qf("250", at=_SEAL + timedelta(seconds=1), basis=BASIS_LAST)]},  # future
        )
        ctx = asyncio.run(assembler.capture([MSFT]))
        assert MSFT not in ctx.prices  # look-ahead withheld -> absent

    def test_available_holding_with_mark_after_seal_is_rejected(self):
        # The domain RiskContext validator rejects an AVAILABLE holding whose mark is after the seal.
        assembler, _ = _assembler(
            held=[_held(AAPL, "AAPL", 10, mv="1500", mark="150", mark_at=_SEAL + timedelta(seconds=1))],
            quotes={},
        )
        with pytest.raises(ValueError):
            asyncio.run(assembler.capture([]))


class TestGenerationFence:
    def test_reconnect_midcapture_is_rejected(self):
        session = FakeSession(generation=0)

        class _ReconnectingFeed(FakeMarketDataFeed):
            async def quotes(self, requested):
                session.bump_generation()  # a reconnect lands between the snapshot and the quote read
                return await super().quotes(requested)

        gateway = FakeAccountGateway(
            clock=FixedClock(_SEAL), summary=_summary(), held=[_held(MSFT, "MSFT", -4)], session=session
        )
        feed = _ReconnectingFeed(quotes={}, session=session)
        asyncio.run(gateway.connect())
        assembler = DecisionContextAssembler(gateway, feed, decision_time_source=FixedDecisionClock(_SEAL))
        with pytest.raises(GenerationFenceError):
            asyncio.run(assembler.capture([MSFT]))

    def test_the_fenced_generation_is_sealed_into_the_context(self):
        # The fence proved account/quotes/session agree on a generation — and then the value was
        # emitted to telemetry and DROPPED. Downstream had nothing to read, so a planner binding "the
        # generation this decision was made at" bound None forever while looking right (2026-07-16).
        # Sealing it in is what makes that binding a real read of a real value (ADR-0002 ⑨).
        session = FakeSession(generation=5)
        gateway = FakeAccountGateway(
            clock=FixedClock(_SEAL), summary=_summary(), held=[_held(MSFT, "MSFT", -4)], session=session
        )
        feed = FakeMarketDataFeed(quotes={}, session=session)
        asyncio.run(gateway.connect())
        assembler = DecisionContextAssembler(gateway, feed, decision_time_source=FixedDecisionClock(_SEAL))
        context = asyncio.run(assembler.capture([MSFT]))
        assert context.generation == 5


class TestDigestDeterminism:
    def test_same_inputs_same_digest_change_on_drift(self):
        def digest_for(qty):
            assembler, _ = _assembler(
                held=[_held(AAPL, "AAPL", qty, mv="1500", mark="150", mark_at=_SEAL - timedelta(seconds=1))],
                quotes={},
            )
            return asyncio.run(assembler.capture([])).context_digest

        assert digest_for(10) == digest_for(10)
        assert digest_for(10) != digest_for(11)
