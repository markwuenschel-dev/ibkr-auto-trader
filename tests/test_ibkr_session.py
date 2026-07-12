"""PT-4b session tests — the ib_async-free session seam: generation, pacing, inventory reconcile.

The real socket-owning ``IbkrSession`` lives in ``ibkr_session.py`` (ib_async, integration-only); these
cover the CI-safe parts: the generation the fence keys on, the modeled pacing gate, and the
portfolio-vs-position reconcile that fails a snapshot closed rather than valuing an unverifiable book.
"""

from __future__ import annotations

import asyncio

from ibkr_trader.ibkr import FakeSession, PacingGate
from ibkr_trader.ibkr.session import Session, aggregate_signed, reconcile_inventory

AAPL, MSFT = 265598, 272093


class TestGeneration:
    def test_bump_is_monotonic(self):
        session = FakeSession(generation=0)
        assert session.generation == 0
        session.bump_generation()
        session.bump_generation()
        assert session.generation == 2

    def test_fake_session_satisfies_protocol(self):
        assert isinstance(FakeSession(), Session)

    def test_run_serialized_returns_value(self):
        session = FakeSession()

        async def work():
            return 42

        assert asyncio.run(session.run_serialized(work)) == 42


class TestPacingGate:
    def test_charges_per_class_cost_default_one(self):
        gate = PacingGate(class_cost={"market_data": 3})
        assert gate.charge("market_data") == 3
        assert gate.charge("account_summary") == 1  # unknown class costs 1
        snap = gate.snapshot()
        assert snap["requests"] == 2
        assert snap["accumulated_cost"] == 4


class TestInventoryReconcile:
    def test_matching_inventory_reconciles(self):
        ok, mismatched = reconcile_inventory({AAPL: 10, MSFT: -4}, {AAPL: 10, MSFT: -4})
        assert ok and mismatched == []

    def test_zero_quantities_are_not_a_mismatch(self):
        ok, mismatched = reconcile_inventory({AAPL: 10}, {AAPL: 10, MSFT: 0})
        assert ok and mismatched == []

    def test_quantity_mismatch_is_reported(self):
        ok, mismatched = reconcile_inventory({AAPL: 10}, {AAPL: 8})
        assert not ok and mismatched == [AAPL]

    def test_missing_instrument_is_a_mismatch(self):
        ok, mismatched = reconcile_inventory({AAPL: 10, MSFT: -4}, {AAPL: 10})
        assert not ok and mismatched == [MSFT]

    def test_aggregate_signed_sums_duplicate_rows(self):
        assert aggregate_signed([(AAPL, 5), (AAPL, 5), (MSFT, -4)]) == {AAPL: 10, MSFT: -4}
