"""Pure, fail-closed portfolio projection used by planning and approval."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from ibkr_trader.domain.models import RiskContext, Side, ValuationStatus


class UnpricedHoldingError(ValueError):
    """The portfolio cannot safely be projected from an incomplete valuation."""


class VerifiedProjection(BaseModel):
    """A recomputable projection; planner copies are explicitly non-authoritative."""

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


class PortfolioProjector:
    """Deep module containing the portfolio arithmetic and no sizing policy."""

    def project(self, order_terms: Any, context: RiskContext, control_state: Any) -> VerifiedProjection:
        # An unavailable position must not disappear from the leverage denominator.
        if any(h.status is ValuationStatus.UNAVAILABLE for h in context.holdings.values()):
            raise UnpricedHoldingError("cannot project a portfolio containing an UNAVAILABLE holding")

        instrument = _get(order_terms, "instrument_id", _get(order_terms, "con_id"))
        if instrument is None:
            instrument = _get(order_terms, "symbol")
        quantity = int(_get(order_terms, "quantity", 0))
        side = Side(_get(order_terms, "side", Side.BUY))
        price_raw = _get(order_terms, "price")
        if price_raw is None and isinstance(instrument, int):
            price_raw = context.prices.get(instrument)
        price = Decimal(str(price_raw)) if price_raw is not None else Decimal(0)
        multiplier = Decimal(str(_get(order_terms, "multiplier", 1)))
        stop_raw = _get(order_terms, "stop_price")
        stop = Decimal(str(stop_raw)) if stop_raw is not None else None
        if (
            quantity <= 0
            or not price.is_finite()
            or price <= 0
            or not multiplier.is_finite()
            or multiplier <= 0
        ):
            raise ValueError("quantity, price, and multiplier must be positive finite values")
        if stop is not None and (not stop.is_finite() or stop <= 0):
            raise ValueError("stop_price must be positive and finite")

        notional = Decimal(quantity) * price * multiplier
        signed_order = notional if side is Side.BUY else -notional
        existing = context.holdings.get(instrument) if isinstance(instrument, int) else None
        existing_mv = existing.broker_market_value if existing is not None else Decimal(0)
        assert existing_mv is not None
        resulting_mv = existing_mv + signed_order
        gross = sum((abs(h.broker_market_value or Decimal(0)) for h in context.holdings.values()), Decimal(0))
        resulting_gross = gross - abs(existing_mv) + abs(resulting_mv)
        nlv = context.net_liquidation
        if not nlv.is_finite() or nlv <= 0:
            raise ValueError("net_liquidation must be positive and finite")
        # Debit is the increase in broker exposure, not the order's face value.
        debit = max(Decimal(0), abs(resulting_mv) - abs(existing_mv))
        stop_distance = abs(price - stop) if stop is not None else Decimal(0)
        max_loss = Decimal(quantity) * stop_distance * multiplier
        return VerifiedProjection(
            notional=notional,
            incremental_buying_power_debit=debit,
            resulting_gross_leverage=resulting_gross / nlv,
            maintenance_headroom=context.maintenance_margin - notional,
            resulting_concentration=abs(resulting_mv) / nlv,
            max_loss_if_stopped=max_loss,
        )
