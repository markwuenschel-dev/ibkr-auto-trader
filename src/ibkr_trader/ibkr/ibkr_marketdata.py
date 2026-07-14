"""ibkr_marketdata — the real ``MarketDataFeed`` backed by ib_async. Imports ib_async (quarantined here).

Snapshot-pull (ADR-0002 ⑧): one point-in-time read per instrument per cycle, keyed to the cycle — no
ticker stream (deferred). Each field's ``available_at`` is the provider *receipt* time (this read's clock),
the only signal the causal gate keys on; IBKR's Mark price / last / prior close feed the precedence. This
module is exercised only by the opt-in integration test, never in CI (lazy ``__getattr__`` in the package).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.models import InstrumentId
from .gateway import Clock
from .marketdata import (
    BASIS_BROKER_MARK,
    BASIS_CLOSE,
    BASIS_LAST,
    QuoteField,
    _BaseMarketDataFeed,
)
from .session import Session


def _dec(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation, ValueError:
        return None
    # ib_async reports an absent quote as NaN; a NaN Decimal is not a usable valuation.
    return None if value.is_nan() else value


class IbkrMarketDataFeed(_BaseMarketDataFeed):
    """The live snapshot feed. Resolves each requested conId to a contract, pulls a snapshot ticker, and
    emits the Mark/last/close candidates that carry a usable value — each stamped with this read's receipt
    time as ``available_at`` (a field with no trustworthy availability is simply omitted, never guessed).
    """

    def __init__(self, ib: Any, *, session: Session | None = None, clock: Clock | None = None) -> None:
        super().__init__(session=session, clock=clock)
        self._ib: Any = ib

    async def _fetch_candidates(
        self, requested: Iterable[InstrumentId]
    ) -> Mapping[InstrumentId, Iterable[QuoteField]]:
        from ib_async import Contract  # quarantined import, lazy so CI never loads ib_async

        out: dict[InstrumentId, list[QuoteField]] = {}
        received_at = self._receipt_time()
        for instrument_id in requested:
            con_id = int(instrument_id)
            details = await self._ib.reqContractDetailsAsync(Contract(conId=con_id))
            if not details:
                continue
            contract = details[0].contract
            ticker = await self._ib.reqTickersAsync(contract)
            if not ticker:
                continue
            fields = self._candidates_from_ticker(ticker[0], received_at)
            if fields:
                out[con_id] = fields
        return out

    def _candidates_from_ticker(self, ticker: object, received_at: datetime) -> list[QuoteField]:
        fields: list[QuoteField] = []
        mark = _dec(getattr(ticker, "markPrice", None))
        if mark is not None:
            fields.append(QuoteField(value=mark, available_at=received_at, basis=BASIS_BROKER_MARK))
        last = _dec(getattr(ticker, "last", None))
        if last is not None:
            event_at = getattr(ticker, "time", None)
            if isinstance(event_at, datetime) and event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=UTC)
            fields.append(
                QuoteField(value=last, available_at=received_at, basis=BASIS_LAST, event_at=event_at)
            )
        close = _dec(getattr(ticker, "close", None))
        if close is not None:
            # Prior-day adjusted close: its event_at cannot be invented from arrival, so it carries none.
            fields.append(QuoteField(value=close, available_at=received_at, basis=BASIS_CLOSE))
        return fields

    def _receipt_time(self) -> datetime:
        clock = self._clock
        if clock is not None:
            return clock.now()  # type: ignore[attr-defined]
        return datetime.now(UTC)
