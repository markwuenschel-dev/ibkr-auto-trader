"""Pure, fail-closed portfolio projection used by planning and approval.

This module deliberately contains no policy decisions and never changes an order.  It
is run once by the planner and again by the approver; the latter run is authoritative.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

from ibkr_trader.domain.models import RiskContext, Side, ValuationStatus


class UnpricedHoldingError(ValueError):
    """The portfolio cannot safely be projected from an incomplete valuation."""


class VerifiedProjection(BaseModel):
    """Recomputable projection (a planner copy is explicitly not authority)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notional: Decimal
    incremental_buying_power_debit: Decimal
    resulting_gross_leverage: Decimal
    maintenance_headroom: Decimal
    resulting_concentration: Decimal
    max_loss_if_stopped: Decimal


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a Decimal-compatible value") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


class PortfolioProjector:
    """Deep module containing portfolio arithmetic and no sizing policy."""

    def project(self, order_terms: Any, context: RiskContext, control_state: Any) -> VerifiedProjection:
        # Never turn an incomplete inventory into a zero in the leverage denominator.
        if any(h.status == ValuationStatus.UNAVAILABLE for h in context.holdings.values()):
            raise UnpricedHoldingError("cannot project a portfolio containing an UNAVAILABLE holding")

        instrument = _get(order_terms, "instrument_id", _get(order_terms, "con_id"))
        if instrument is None:
            instrument = _get(order_terms, "symbol")
        quantity_raw = _get(order_terms, "quantity")
        try:
            quantity = int(quantity_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity must be a positive integer") from exc
        side = Side(_get(order_terms, "side", Side.BUY))

        price_raw = _get(order_terms, "price")
        if price_raw is None and instrument in context.prices:
            price_raw = context.prices[instrument]
        price = _decimal(price_raw, "price")
        multiplier = _decimal(_get(order_terms, "multiplier", 1), "multiplier")
        stop_raw = _get(order_terms, "stop_price")
        stop = _decimal(stop_raw, "stop_price") if stop_raw is not None else None
        if quantity <= 0 or price <= 0 or multiplier <= 0:
            raise ValueError("quantity, price, and multiplier must be positive finite values")

        # A stop on the safe side is not a stop-loss and must not produce a
        # deceptively small risk number.
        if stop is not None and (
            stop <= 0 or (side == Side.BUY and stop >= price) or (side == Side.SELL and stop <= price)
        ):
            raise ValueError("stop_price is on the wrong side of the order")

        notional = Decimal(quantity) * price * multiplier
        signed_order = notional if side == Side.BUY else -notional
        existing = context.holdings.get(instrument)
        existing_mv = Decimal(0) if existing is None else existing.broker_market_value
        if existing_mv is None or not existing_mv.is_finite():
            raise UnpricedHoldingError("holding has no finite broker market value")
        resulting_mv = existing_mv + signed_order

        gross = Decimal(0)
        for h in context.holdings.values():
            mv = h.broker_market_value
            if mv is None or not mv.is_finite():
                raise UnpricedHoldingError("holding has no finite broker market value")
            gross += abs(mv)
        resulting_gross = gross - abs(existing_mv) + abs(resulting_mv)
        nlv = _decimal(context.net_liquidation, "net_liquidation")
        if nlv <= 0:
            raise ValueError("net_liquidation must be positive")

        # Buying power is the increase in absolute broker exposure.  In
        # particular, selling/reducing a position has no debit, and a short
        # order is not confused with a negative notional.
        debit = max(Decimal(0), abs(resulting_mv) - abs(existing_mv))
        stop_distance = Decimal(0) if stop is None else abs(price - stop)
        max_loss = Decimal(quantity) * stop_distance * multiplier
        return VerifiedProjection(
            notional=notional,
            incremental_buying_power_debit=debit,
            resulting_gross_leverage=resulting_gross / nlv,
            maintenance_headroom=_decimal(context.maintenance_margin, "maintenance_margin") - notional,
            resulting_concentration=abs(resulting_mv) / nlv,
            max_loss_if_stopped=max_loss,
        )
