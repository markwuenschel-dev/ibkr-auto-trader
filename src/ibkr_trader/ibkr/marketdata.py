"""marketdata — the ib_async-free market-data port, the raw quote types, and the pure causal gate (PT-4c).

ADR-0002 fixes the shape: ``MarketDataFeed.quotes(requested) -> QuoteBatch`` returns *raw* ordered
candidate fields per instrument; the ``DecisionContextAssembler`` seals ``decision_at`` and runs the pure
**causal gate** here — select the highest-precedence field whose ``available_at ≤ decision_at`` (strict,
zero tolerance). Causality keys on *availability* time, never event time (Decision ②); a field with no
trustworthy availability metadata is **absent**, never guessed. This module is ib_async-free (Decision ⑦);
the real feed lives in ``ibkr_marketdata.py`` (lazy import), the fake in ``fake_marketdata.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.models import InstrumentId
from .gateway import Clock, _as_utc
from .session import Session

# Valuation bases (ADR-0002 ⑪), in precedence order: broker Mark → last trade → prior close. No ``mid``.
BASIS_BROKER_MARK = "BROKER_MARK"
BASIS_LAST = "LAST"
BASIS_CLOSE = "CLOSE"
BASIS_PRECEDENCE: tuple[str, ...] = (BASIS_BROKER_MARK, BASIS_LAST, BASIS_CLOSE)

# Telemetry (§8): a field withheld for look-ahead, and a stale-but-causal field.
STAGE_MD_SNAPSHOT = "md.snapshot"
STAGE_MD_LOOKAHEAD = "md.lookahead"
STAGE_MD_STALE = "md.stale"
STAGE_MD_UNAVAILABLE = "md.unavailable"


@dataclass(frozen=True)
class QuoteField:
    """One candidate valuation datum for an instrument.

    ``available_at`` is provider *receipt* time in live / explicit in replay — the only field the causal
    gate keys on. ``event_at`` (when the datum came to be, e.g. a last-trade time) is a *separate*,
    optional staleness signal; ``basis`` records which field this is (drives precedence + audit).
    """

    value: object  # Decimal; kept ``object`` so the assembler does the string->Decimal parse + validation
    available_at: datetime
    basis: str
    event_at: datetime | None = None


@dataclass(frozen=True)
class QuoteBatch:
    """Raw ordered candidate fields per instrument, tagged with the session ``generation`` at collection.

    ``quotes[instrument]`` is ordered by basis precedence (index 0 = most preferred). The gate selects the
    first causal field; the batch itself withholds nothing (raw), so the causal reasoning is one place.
    """

    quotes: Mapping[InstrumentId, tuple[QuoteField, ...]]
    generation: int
    collected_at: datetime


@runtime_checkable
class MarketDataFeed(Protocol):
    """The market-data port (Decision ⑦). Snapshot-pull: one read per cycle, one seal. ib_async-free."""

    async def quotes(self, requested: Iterable[InstrumentId]) -> QuoteBatch: ...
    @property
    def generation(self) -> int: ...


class _BaseMarketDataFeed:
    """Template for the real/fake feeds: order candidates by basis precedence, tag with the generation.

    Subclasses implement ``_fetch_candidates(requested) -> Mapping[InstrumentId, Iterable[QuoteField]]``
    returning raw (unordered, un-gated) fields; the base sorts each instrument's fields into precedence
    order and stamps the batch with ``generation`` from the injected session. No look-ahead decision here —
    that is the assembler's sealed causal gate.
    """

    def __init__(self, *, session: Session | None = None, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock

    @property
    def generation(self) -> int:
        session = self._session
        return int(session.generation) if session is not None else 0

    async def _fetch_candidates(
        self, requested: Iterable[InstrumentId]
    ) -> Mapping[InstrumentId, Iterable[QuoteField]]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _now(self) -> datetime:
        clock = self._clock
        if clock is not None:
            return clock.now()
        from datetime import UTC

        return datetime.now(UTC)

    async def quotes(self, requested: Iterable[InstrumentId]) -> QuoteBatch:
        raw = await self._fetch_candidates(list(requested))
        ordered: dict[InstrumentId, tuple[QuoteField, ...]] = {}
        for instrument_id, fields in raw.items():
            ordered[int(instrument_id)] = order_by_precedence(fields)
        return QuoteBatch(quotes=ordered, generation=self.generation, collected_at=self._now())


def order_by_precedence(fields: Iterable[QuoteField]) -> tuple[QuoteField, ...]:
    """Order candidate fields by valuation basis precedence (BROKER_MARK → LAST → CLOSE), unknown last.

    Stable within a basis, so a provider that supplies two fields of the same basis keeps their order.
    """

    def rank(field: QuoteField) -> int:
        try:
            return BASIS_PRECEDENCE.index(field.basis)
        except ValueError:
            return len(BASIS_PRECEDENCE)

    return tuple(sorted(fields, key=rank))


@dataclass(frozen=True)
class CausalSelection:
    """The gate's result for one instrument: the chosen causal field (if any) + withheld look-ahead ones."""

    chosen: QuoteField | None
    withheld: tuple[QuoteField, ...]


def select_causal(fields: Iterable[QuoteField], decision_at: datetime) -> CausalSelection:
    """Pure causal gate (ADR-0002 ②/⑥): pick the highest-precedence field with ``available_at ≤ decision_at``.

    Strict, zero tolerance (no epsilon). ``available_at`` and ``decision_at`` are tz-normalized to UTC via
    ``_as_utc`` before comparison. Fields ordered later than the seal are *withheld* (a ``md.lookahead``
    signal); if none is causal the instrument is absent — never a guessed or fabricated price.
    """
    seal = _as_utc(decision_at)
    chosen: QuoteField | None = None
    withheld: list[QuoteField] = []
    for field in order_by_precedence(fields):
        if _as_utc(field.available_at) <= seal:
            if chosen is None:
                chosen = field
        else:
            withheld.append(field)
    return CausalSelection(chosen=chosen, withheld=tuple(withheld))
