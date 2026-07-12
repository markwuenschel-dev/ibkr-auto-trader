"""fake_marketdata — the in-memory ``MarketDataFeed`` for CI, doubling as the deterministic replay feed.

No socket, no ib_async. Hand-set raw ``QuoteField`` candidates per instrument (each with an explicit
``available_at`` — real receipt time in live, scheduled historical time in replay) run through the *same*
``_BaseMarketDataFeed`` template + causal gate the live feed uses, so the causal reasoning CI exercises is
the production reasoning. Share a ``FakeSession`` with a ``FakeAccountGateway`` to drive the assembler's
reconnect fence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..domain.models import InstrumentId
from .gateway import Clock
from .marketdata import QuoteField, _BaseMarketDataFeed
from .session import Session


class FakeMarketDataFeed(_BaseMarketDataFeed):
    """A deterministic feed backed by a hand-set ``{conId: [QuoteField, ...]}`` map.

    Only the *requested* instruments that the feed knows about are returned; an unknown requested
    instrument is simply absent (the assembler seeds held valuation from the account snapshot, and a
    genuinely unpriced instrument stays out of ``prices`` — never fabricated).
    """

    def __init__(
        self,
        *,
        quotes: Mapping[InstrumentId, Iterable[QuoteField]] | None = None,
        session: Session | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(session=session, clock=clock)
        self.candidates: dict[InstrumentId, list[QuoteField]] = {
            int(k): list(v) for k, v in (quotes or {}).items()
        }

    async def _fetch_candidates(
        self, requested: Iterable[InstrumentId]
    ) -> Mapping[InstrumentId, Iterable[QuoteField]]:
        out: dict[InstrumentId, Iterable[QuoteField]] = {}
        for instrument_id in requested:
            fields = self.candidates.get(int(instrument_id))
            if fields:
                out[int(instrument_id)] = list(fields)
        return out
