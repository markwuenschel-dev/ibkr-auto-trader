"""Regression tests for the PT-4b conId reconciliation boundary.

After the PT-4b break, held inventory carries the broker ``conId`` (ADR-0002 ⑫/⑬) and the store is
conId-keyed, so reconciliation is identity-vs-identity — no symbol translation, no manufactured id. Broker
is truth: divergence rewrites the cache by conId (carrying the display symbol) and never gates the read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ibkr_trader.ibkr import FakeAccountGateway, FixedClock, HeldPosition
from ibkr_trader.state import StateStore

_MOMENT = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
AAPL, MSFT, TSLA = 265598, 272093, 76792991


def _held(con_id: int, symbol: str, quantity: int) -> HeldPosition:
    return HeldPosition.from_broker(
        instrument_id=con_id,
        symbol=symbol,
        quantity=quantity,
        market_value=None,
        market_price=None,
        mark_available_at=None,
    )


def _summary() -> dict[str, str]:
    return {"NetLiquidation": "2000.50", "BuyingPower": "8000.00", "MaintMarginReq": "150.25"}


def test_reconcile_is_conid_vs_conid_and_broker_is_truth(tmp_path) -> None:
    with StateStore(tmp_path / "trader.db") as store:
        store.upsert_position(AAPL, "AAPL", 5)  # cache lags broker
        store.upsert_position(TSLA, "TSLA", 3)  # broker no longer holds this
        gateway = FakeAccountGateway(
            clock=FixedClock(_MOMENT),
            summary=_summary(),
            held=[_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4)],
            store=store,
        )
        asyncio.run(gateway.connect())
        asyncio.run(gateway.snapshot())

        recon = gateway.last_reconciliation
        assert recon is not None and recon.diverged
        assert recon.diffs == {AAPL: (5, 10), MSFT: (0, -4), TSLA: (3, 0)}
        # Broker truth written back by conId; TSLA (absent at broker) goes flat, never invented away.
        assert store.all_positions() == {AAPL: 10, MSFT: -4, TSLA: 0}
        assert store.symbol_for_instrument_id(MSFT) == "MSFT"


def test_matching_inventory_does_not_diverge(tmp_path) -> None:
    with StateStore(tmp_path / "trader.db") as store:
        store.upsert_position(AAPL, "AAPL", 10)
        gateway = FakeAccountGateway(
            clock=FixedClock(_MOMENT),
            summary=_summary(),
            held=[_held(AAPL, "AAPL", 10)],
            store=store,
        )
        asyncio.run(gateway.connect())
        asyncio.run(gateway.snapshot())
        recon = gateway.last_reconciliation
        assert recon is not None and not recon.diverged
        assert store.all_positions() == {AAPL: 10}
