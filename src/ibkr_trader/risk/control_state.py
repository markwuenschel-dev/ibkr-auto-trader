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
