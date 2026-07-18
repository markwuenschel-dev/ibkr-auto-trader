"""Risk-owned sizing: a plan is a fixed proposal, never an authority claim."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from ibkr_trader.domain.models import RiskContext, RiskPlan, Side
from ibkr_trader.risk.control_state import RiskControlState
from ibkr_trader.risk.order_terms import OrderTerms
from ibkr_trader.risk.projector import PortfolioProjector, UnpricedHoldingError


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _whole(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding=ROUND_DOWN)))


def _round_lot(quantity: int, lot_size: Any) -> int:
    lot = int(lot_size or 1)
    if lot <= 0:
        raise ValueError("lot_size must be positive")
    return quantity - quantity % lot


class RiskPlanner:
    """Selects one fixed candidate; downstream approval never resizes it."""

    def __init__(self, projector: PortfolioProjector | None = None) -> None:
        self.projector = projector or PortfolioProjector()

    def plan(self, intent: Any, context: RiskContext, control_state: RiskControlState) -> RiskPlan:
        # `intent` is deliberately Any: the declared StrategyIntent (symbol/target_weight/rationale)
        # is NOT the runtime shape — plan() reads ~10 fields off it, so its real type is the unbuilt
        # PT-strategy rebalancer contract. Typing it is deferred to INT-006b/wayfinder rather than
        # invented here. `context` and `control_state` ARE typed: that is the seam this slice closes.
        policy = control_state.policy
        instrument = _get(intent, "instrument_id", _get(intent, "con_id"))
        if instrument is None:
            instrument = _get(intent, "symbol")
        symbol = str(_get(intent, "symbol", instrument))
        side = Side(_get(intent, "side", Side.BUY))
        stop_raw = _get(intent, "stop_price")
        stop = _decimal(stop_raw, "stop_price") if stop_raw is not None else None
        price_raw = _get(intent, "price")
        if price_raw is None:
            price_raw = context.prices.get(instrument)
        price = _decimal(price_raw, "price") if price_raw is not None else Decimal(0)

        requested_raw = _get(intent, "quantity")
        if requested_raw is None:
            weight = _decimal(_get(intent, "target_weight", 0), "target_weight")
            requested = _whole(abs(weight) * context.net_liquidation / price) if price > 0 else 0
        else:
            try:
                requested = int(requested_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("quantity must be an integer") from exc
        requested = _round_lot(max(0, requested), _get(intent, "lot_size", 1))

        quantity = requested
        reason: str | None = None
        if policy.stop_loss_required and stop is None:
            quantity, reason = 0, "stop-loss-required"
        elif quantity == 0:
            reason = "zero-size"
        elif stop is not None:
            unit_loss = abs(price - stop) * _decimal(_get(intent, "multiplier", 1), "multiplier")
            if unit_loss <= 0 or not unit_loss.is_finite() or price <= 0:
                quantity, reason = 0, "invalid-stop"
            else:
                maximum = _decimal(policy.max_risk_per_trade, "max_risk_per_trade") * context.net_liquidation
                # This is intentionally one-shot sizing, not a replan loop.
                quantity = _round_lot(min(quantity, _whole(maximum / unit_loss)), _get(intent, "lot_size", 1))
                if quantity < requested:
                    reason = "reduced-to-max-risk"

        projection = None
        if quantity:
            # Build the typed OrderTerms inside the try: a malformed field is a pydantic
            # ValidationError (a ValueError subclass), so the planner still declines gracefully rather
            # than crashing — same contract the projection failure path already had (INT-006).
            try:
                terms = OrderTerms(
                    instrument_id=instrument,
                    quantity=quantity,
                    side=side,
                    price=price,
                    stop_price=stop,
                    multiplier=_decimal(_get(intent, "multiplier", 1), "multiplier"),
                )
                projection = self.projector.project(terms, context, control_state)
            except (ValueError, UnpricedHoldingError) as exc:
                quantity, reason = 0, str(exc)

        # Read the REAL fields, not duck-typed lookups. `_get(context, "generation", ...)` silently
        # returned None because neither RiskContext nor RiskControlState declared the attribute: the
        # binding looked present in the diff and bound nothing. Both are now typed fields, so a typo
        # here is a pyright error rather than a plan that lies quietly (ADR-0003; 2026-07-16).
        return RiskPlan(
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_price=stop if stop is not None else Decimal(0),
            est_risk_amount=projection.max_loss_if_stopped if projection else Decimal(0),
            planner_projection=projection,
            projection_is_authoritative=False,
            context_digest=context.context_digest,
            decision_generation=context.generation,  # the generation the context was SEALED at
            session_generation=control_state.session_generation,  # ...and the one seen at plan time
            policy_version=policy.version,
            declined=quantity == 0,
            decline_reason=reason,
        )
