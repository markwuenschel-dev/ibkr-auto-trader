"""Control-plane facts consumed by Risk & Sizing.

This state is intentionally separate from the assembler-sealed ``RiskContext``:
its reviewed policy and session P&L have distinct owners and freshness rules.
Mode remains with ModeController, while reservations and idempotency remain with
Execution Control.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from ibkr_trader.config import RiskPolicy
from ibkr_trader.domain.models import _Frozen


class RiskControlState(_Frozen):
    """The reviewed policy and session-scoped facts for one risk evaluation."""

    policy: RiskPolicy
    session_date: date
    realized_daily_pnl: Decimal
    observed_at: datetime
    #: The session generation (ADR-0002 ⑨) observed when this control state was built. Bumped on every
    #: reconnect. A plan binds this alongside the sealed context's generation: when the two differ, the
    #: session reconnected between the decision seal and planning, so the plan mixes pre- and
    #: post-reconnect state. Required and typed — the whole point of PT-6's generation binding is that
    #: it reads a real value, and a defaulted field would let "no generation" masquerade as one.
    session_generation: int
