"""PT-4c market-data tests — the pure causal gate + the fake feed, all ib_async-free (ADR-0002 ②/⑥/⑦).

Causality keys on availability time, strict and zero-tolerance; a field later than the seal is withheld,
a field with no causal datum is absent (never guessed). Staleness (``event_at``) is a separate concern.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ibkr_trader.ibkr import FakeMarketDataFeed, FakeSession, QuoteField, select_causal
from ibkr_trader.ibkr.marketdata import (
    BASIS_BROKER_MARK,
    BASIS_CLOSE,
    BASIS_LAST,
    order_by_precedence,
)

_SEAL = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
AAPL, MSFT = 265598, 272093


def _qf(value: str, *, at: datetime, basis: str, event_at: datetime | None = None) -> QuoteField:
    return QuoteField(value=Decimal(value), available_at=at, basis=basis, event_at=event_at)


class TestCausalGate:
    def test_accepts_available_at_or_before_seal(self):
        field = _qf("150", at=_SEAL, basis=BASIS_LAST)  # exactly at the seal is causal (≤, not <)
        selection = select_causal([field], _SEAL)
        assert selection.chosen is field
        assert selection.withheld == ()

    def test_withholds_field_after_seal(self):
        later = _qf("151", at=_SEAL + timedelta(seconds=1), basis=BASIS_LAST)
        selection = select_causal([later], _SEAL)
        assert selection.chosen is None  # absent — never guessed
        assert selection.withheld == (later,)

    def test_precedence_prefers_broker_mark_among_causal(self):
        close = _qf("100", at=_SEAL - timedelta(minutes=5), basis=BASIS_CLOSE)
        last = _qf("150", at=_SEAL - timedelta(seconds=10), basis=BASIS_LAST)
        mark = _qf("149", at=_SEAL - timedelta(seconds=1), basis=BASIS_BROKER_MARK)
        selection = select_causal([close, last, mark], _SEAL)
        assert selection.chosen is mark  # BROKER_MARK wins precedence, not the most recent

    def test_falls_through_precedence_when_preferred_is_look_ahead(self):
        # BROKER_MARK reported with a future availability is withheld; LAST (causal) is chosen.
        mark_future = _qf("149", at=_SEAL + timedelta(seconds=2), basis=BASIS_BROKER_MARK)
        last = _qf("150", at=_SEAL - timedelta(seconds=10), basis=BASIS_LAST)
        selection = select_causal([mark_future, last], _SEAL)
        assert selection.chosen is last
        assert mark_future in selection.withheld

    def test_post_open_pre_seal_tick_is_accepted(self):
        # The anti-starvation invariant: a tick that arrives between collection-open and the seal is causal.
        fresh = _qf("152", at=_SEAL - timedelta(milliseconds=5), basis=BASIS_LAST)
        assert select_causal([fresh], _SEAL).chosen is fresh

    def test_no_causal_datum_is_absent(self):
        assert select_causal([], _SEAL).chosen is None

    def test_staleness_is_not_lookahead(self):
        # A causal field whose event_at is old is still selected (staleness is a separate signal).
        stale = _qf(
            "150", at=_SEAL - timedelta(seconds=1), basis=BASIS_LAST, event_at=_SEAL - timedelta(days=3)
        )
        assert select_causal([stale], _SEAL).chosen is stale

    def test_tz_normalizes_naive_availability(self):
        naive = _qf("150", at=(_SEAL - timedelta(seconds=1)).replace(tzinfo=None), basis=BASIS_LAST)
        assert select_causal([naive], _SEAL).chosen is naive


class TestPrecedenceOrdering:
    def test_orders_broker_mark_last_close_unknown(self):
        close = _qf("1", at=_SEAL, basis=BASIS_CLOSE)
        last = _qf("2", at=_SEAL, basis=BASIS_LAST)
        mark = _qf("3", at=_SEAL, basis=BASIS_BROKER_MARK)
        unknown = _qf("4", at=_SEAL, basis="MID")
        ordered = order_by_precedence([unknown, close, mark, last])
        assert [f.basis for f in ordered] == [BASIS_BROKER_MARK, BASIS_LAST, BASIS_CLOSE, "MID"]


class TestFakeFeed:
    def test_returns_only_requested_known_instruments_ordered(self):
        feed = FakeMarketDataFeed(
            quotes={
                AAPL: [
                    _qf("100", at=_SEAL, basis=BASIS_CLOSE),
                    _qf("150", at=_SEAL, basis=BASIS_BROKER_MARK),
                ]
            },
            session=FakeSession(generation=0),
        )
        batch = asyncio.run(feed.quotes([AAPL, MSFT]))  # MSFT unknown -> absent
        assert set(batch.quotes) == {AAPL}
        assert [f.basis for f in batch.quotes[AAPL]] == [BASIS_BROKER_MARK, BASIS_CLOSE]
        assert batch.generation == 0

    def test_generation_tracks_shared_session(self):
        session = FakeSession(generation=2)
        feed = FakeMarketDataFeed(quotes={}, session=session)
        assert asyncio.run(feed.quotes([])).generation == 2
        session.bump_generation()
        assert asyncio.run(feed.quotes([])).generation == 3
