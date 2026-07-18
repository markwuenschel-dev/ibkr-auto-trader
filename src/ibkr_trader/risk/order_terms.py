"""Canonical sized-order terms — the typed contract between the planner and the projector.

Built by ``RiskPlanner`` after reduction/rounding and consumed by ``PortfolioProjector``; the PT-5
approver re-projects from the same shape over the sealed context (ADR-0003). This replaces the
stringly-keyed dict the two used to exchange, so a mis-keyed field is a construction error rather than
a silent ``_get`` default on the money path (INT-006). Identity is the resolved ``InstrumentId`` only —
symbol resolution is an upstream concern (ADR-0002); the projector never re-resolves identity.
"""

from __future__ import annotations

from decimal import Decimal

from ibkr_trader.domain.models import InstrumentId, Side, StrictDecimal, _Frozen


class OrderTerms(_Frozen):
    """One sized, priced order the projector can turn into a VerifiedProjection."""

    instrument_id: InstrumentId
    quantity: int
    side: Side
    price: StrictDecimal
    #: Optional: the planner may project a position with no stop (when policy does not require one);
    #: when present, the projector still rejects a stop on the safe side.
    stop_price: StrictDecimal | None = None
    #: Contract multiplier (1 for equities). StrictDecimal so a float never enters the notional math.
    multiplier: StrictDecimal = Decimal(1)
