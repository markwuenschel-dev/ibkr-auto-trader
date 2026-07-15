"""Risk-owned sizing.  A plan is a proposal, never an authority claim."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from ibkr_trader.domain.models import RiskContext, RiskPlan, Side, StrategyIntent
from ibkr_trader.risk.projector import PortfolioProjector, UnpricedHoldingError


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _whole(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding=ROUND_DOWN)))


class RiskPlanner:
    """Selects one fixed candidate; it never asks a downstream component to resize."""

    def __init__(self, projector: PortfolioProjector | None = None) -> None:
        self.projector = projector or PortfolioProjector()

    def plan(self, intent: StrategyIntent | Any, context: RiskContext, control_state: Any) -> RiskPlan:
        policy = control_state.policy
        instrument = _get(intent, "instrument_id", _get(intent, "con_id"))
        if instrument is None:
            instrument = _get(intent, "symbol")
        symbol = str(_get(intent, "symbol", instrument))
        side = Side(_get(intent, "side", Side.BUY))
        stop_raw = _get(intent, "stop_price")
        stop = Decimal(str(stop_raw)) if stop_raw is not None else None
        price_raw = _get(intent, "price")
        if price_raw is None and isinstance(instrument, int):
            price_raw = context.prices.get(instrument)
        price = Decimal(str(price_raw)) if price_raw is not None else Decimal(0)
        requested = _get(intent, "quantity")
        if requested is None:
            weight = Decimal(str(_get(intent, "target_weight", 0)))
            requested = _whole(abs(weight) * context.net_liquidation / price) if price > 0 else 0
        requested = max(0, int(requested))
        quantity = requested
        reason = None
        if policy.stop_loss_required and stop is None:
            quantity, reason = 0, "stop-loss-required"
        elif quantity == 0:
            reason = "zero-size"
        else:
            # One-shot reduction: max loss per unit is the policy's hard sizing bound.
            if stop is not None and price > 0:
                unit_loss = abs(price - stop) * Decimal(str(_get(intent, "multiplier", 1)))
                if unit_loss <= 0 or not unit_loss.is_finite():
                    quantity, reason = 0, "invalid-stop"
                else:
                    maximum = policy.max_risk_per_trade * context.net_liquidation
                    quantity = min(quantity, _whole(maximum / unit_loss))
                    if quantity < requested:
                        reason = "reduced-to-max-risk"
            if quantity == 0 and reason is None:
                reason = "risk-limit"

        projection = None
        if quantity:
            terms = {
                "instrument_id": instrument,
                "quantity": quantity,
                "side": side,
                "price": price,
                "stop_price": stop,
                "multiplier": _get(intent, "multiplier", 1),
            }
            try:
                projection = self.projector.project(terms, context, control_state)
            except (ValueError, UnpricedHoldingError) as exc:
                quantity, reason = 0, str(exc)
        generation = _get(context, "generation", _get(control_state, "generation", None))
        return RiskPlan(
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_price=stop or Decimal("0"),
            est_risk_amount=(projection.max_loss_if_stopped if projection else Decimal(0)),
            planner_projection=projection,
            projection_is_authoritative=False,
            context_digest=context.context_digest,
            decision_generation=generation,
            session_generation=_get(control_state, "session_generation", generation),
            policy_version=policy.version,
            declined=quantity == 0,
            decline_reason=reason,
        )
