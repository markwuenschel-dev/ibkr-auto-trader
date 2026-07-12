"""assembler — the ``DecisionContextAssembler``, sole production constructor of ``RiskContext`` (PT-4c).

``capture(requested)`` (ADR-0002 ④):

  1. serially acquire the account snapshot, then the quote batch (no ``gather()`` — one paced session);
  2. **seal ``decision_at`` at the close of collection** via an injected ``decision_time_source`` (live/
     paper inject a real clock, replay its scheduled clock) — never caller-supplied, so live code cannot
     manufacture a convenient historical cutoff;
  3. honour the **generation fence** — reject the whole cycle if the account snapshot, the quote batch, and
     the session at seal disagree on generation (a reconnect mid-capture must never splice a snapshot);
  4. run the pure **causal gate** (``available_at ≤ decision_at``) over the merged candidate fields;
  5. mint the sealed, conId-keyed ``holdings``-map ``RiskContext`` via ``ASSEMBLER_AUTHORITY``.

The mint seam is provenance only — it proves *who* assembled the context, never that assembly was correct
(the PT-5 belt is the suspenders). Held valuation is seeded from the ``AccountSnapshot`` broker mark
(the highest-precedence basis); a genuinely unpriced instrument stays out of ``prices`` (``UNAVAILABLE``
surfaced, never a fabricated zero). ib_async-free.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..domain.models import (
    ASSEMBLER_AUTHORITY,
    HoldingValuation,
    InstrumentId,
    RiskContext,
    ValuationStatus,
)
from ..ibkr.gateway import AccountGateway, AccountSnapshot, HeldPosition, _as_utc
from ..ibkr.marketdata import (
    BASIS_BROKER_MARK,
    STAGE_MD_LOOKAHEAD,
    STAGE_MD_UNAVAILABLE,
    MarketDataFeed,
    QuoteBatch,
    QuoteField,
    select_causal,
)
from ..telemetry import TelemetrySink

STAGE_DECISION_SEAL = "decision.seal"


class GenerationFenceError(Exception):
    """The capture cycle spanned a reconnect (generation bump) — rejected, never a spliced snapshot."""


@runtime_checkable
class DecisionTimeSource(Protocol):
    """Seals ``decision_at`` at the close of collection. ``now()`` returns tz-aware UTC."""

    def now(self) -> datetime: ...


class SystemDecisionClock:
    """The real sealing clock: ``datetime.now(UTC)``."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class DecisionContextAssembler:
    """Deep module; the only production code that constructs ``RiskContext`` (ADR-0002 ④/⑤).

    PT-13 owns cadence + *which* symbols to request and calls ``capture(requested)``; this owns acquisition,
    sealing, causal selection, and assembly.
    """

    def __init__(
        self,
        gateway: AccountGateway,
        feed: MarketDataFeed,
        *,
        decision_time_source: DecisionTimeSource | None = None,
        emitter: TelemetrySink | None = None,
    ) -> None:
        self._gateway = gateway
        self._feed = feed
        self._decision_time_source: DecisionTimeSource = decision_time_source or SystemDecisionClock()
        self._emitter = emitter

    async def capture(self, requested: Iterable[InstrumentId]) -> RiskContext:
        requested_ids = {int(r) for r in requested}
        # (1) serialized acquisition — account snapshot, then quotes (no gather()).
        account: AccountSnapshot = await self._gateway.snapshot()
        held_by_id: dict[InstrumentId, HeldPosition] = {h.instrument_id: h for h in account.held}
        universe = set(held_by_id) | requested_ids
        quotes: QuoteBatch = await self._feed.quotes(universe)

        # (2) seal decision_at AFTER collection.
        decision_at = _as_utc(self._decision_time_source.now())

        # (3) generation fence — account, quotes, and the session at seal must agree.
        gen_at_seal = int(self._gateway.generation)
        if not (account.generation == quotes.generation == gen_at_seal):
            raise GenerationFenceError(
                "capture cycle spanned a reconnect: "
                f"account.generation={account.generation}, quotes.generation={quotes.generation}, "
                f"session_at_seal={gen_at_seal}"
            )

        # holdings map (ADR-0003 D4) — positions derived downstream.
        holdings: dict[InstrumentId, HoldingValuation] = {
            h.instrument_id: HoldingValuation(
                quantity=h.quantity,
                status=h.valuation_status,
                broker_market_value=h.broker_market_value,
                mark_available_at=h.mark_available_at,
            )
            for h in account.held
        }

        # (4) causal gate over merged candidates: the account broker mark (highest precedence) unioned with
        # the feed's fields, per instrument in the decision universe.
        prices: dict[InstrumentId, Decimal] = {}
        price_basis: dict[InstrumentId, str] = {}
        data_as_of: dict[InstrumentId, datetime] = {}
        for instrument_id in universe:
            candidates: list[QuoteField] = []
            held = held_by_id.get(instrument_id)
            if (
                held is not None
                and held.valuation_status is ValuationStatus.AVAILABLE
                and held.broker_mark is not None
                and held.mark_available_at is not None
            ):
                candidates.append(
                    QuoteField(
                        value=held.broker_mark,
                        available_at=held.mark_available_at,
                        basis=BASIS_BROKER_MARK,
                    )
                )
            candidates.extend(quotes.quotes.get(instrument_id, ()))
            selection = select_causal(candidates, decision_at)
            if selection.withheld:
                self._emit(STAGE_MD_LOOKAHEAD, {"instrument": instrument_id, "decision_at": str(decision_at)})
            if selection.chosen is not None:
                prices[instrument_id] = Decimal(str(selection.chosen.value))
                price_basis[instrument_id] = selection.chosen.basis
                data_as_of[instrument_id] = _as_utc(selection.chosen.available_at)
            elif candidates:
                # candidates existed but none was causal — instrument stays out of prices, never guessed.
                self._emit(STAGE_MD_UNAVAILABLE, {"instrument": instrument_id})

        context_digest = _compute_digest(account, holdings, prices, price_basis, data_as_of, decision_at)
        self._emit(
            STAGE_DECISION_SEAL,
            {
                "decision_at": str(decision_at),
                "generation": gen_at_seal,
                "priced": len(prices),
                "held": len(holdings),
            },
        )
        return RiskContext._mint(
            ASSEMBLER_AUTHORITY,
            holdings=holdings,
            net_liquidation=account.net_liquidation,
            buying_power=account.buying_power,
            maintenance_margin=account.maintenance_margin,
            prices=prices,
            price_basis=price_basis,
            data_as_of=data_as_of,
            account_observed_at=account.observed_at,
            as_of=decision_at,
            context_digest=context_digest,
        )

    def _emit(self, stage: str, metrics: dict) -> None:
        emitter = self._emitter
        if emitter is None:
            return
        try:
            emitter.emit(stage=stage, agent_role="decision-assembler", task_id="PT-4", metrics=metrics)
        except Exception:  # observability is best-effort; never break the decision path
            return


def _compute_digest(
    account: AccountSnapshot,
    holdings: dict[InstrumentId, HoldingValuation],
    prices: dict[InstrumentId, Decimal],
    price_basis: dict[InstrumentId, str],
    data_as_of: dict[InstrumentId, datetime],
    decision_at: datetime,
) -> str:
    """A deterministic, order-independent digest over the sealed decision inputs (ADR-0003 D4).

    Covers each holding's quantity/status/market value/mark receipt time, the account scalars, the causal
    prices + bases + availability times, and the seal instant — so any drift changes the digest.
    """
    parts: list[str] = [
        f"nlv={account.net_liquidation}",
        f"bp={account.buying_power}",
        f"mm={account.maintenance_margin}",
        f"observed_at={_iso(account.observed_at)}",
        f"as_of={_iso(decision_at)}",
    ]
    for instrument_id in sorted(holdings):
        h = holdings[instrument_id]
        parts.append(
            f"hold:{instrument_id}|{h.quantity}|{h.status}|{h.broker_market_value}|"
            f"{_iso(h.mark_available_at)}"
        )
    for instrument_id in sorted(prices):
        parts.append(
            f"price:{instrument_id}|{prices[instrument_id]}|{price_basis.get(instrument_id)}|"
            f"{_iso(data_as_of.get(instrument_id))}"
        )
    body = "\n".join(parts)
    return "context:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str:
    if value is None:
        return "none"
    return _as_utc(value).astimezone(UTC).isoformat()
