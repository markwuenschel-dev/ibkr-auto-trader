"""Control-plane configuration for the paper-first trading system.

The mode vocabulary and reviewed risk policy live here.  This module does not
submit orders or talk to a broker; it supplies the inputs consumed by those
components.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from ibkr_trader.domain.models import _Frozen


class Mode(enum.StrEnum):
    """Execution mode; transition policy belongs to ModeController."""

    PAPER = "PAPER"
    LIVE_SMALL_TEST = "LIVE_SMALL_TEST"
    LIVE = "LIVE"
    REDUCE_ONLY = "REDUCE_ONLY"
    PAUSED = "PAUSED"
    KILL_SWITCHED = "KILL_SWITCHED"


_LIVE_MODES = frozenset({Mode.LIVE_SMALL_TEST, Mode.LIVE})
_HALTED_MODES = frozenset({Mode.PAUSED, Mode.KILL_SWITCHED})


def resolve_mode(requested: Mode | None, *, live_enabled: bool = False) -> Mode:
    """Resolve the effective mode, refusing unreviewed LIVE requests to PAPER."""
    if requested is None:
        return Mode.PAPER
    if requested in _LIVE_MODES and not live_enabled:
        return Mode.PAPER
    return requested


def submission_allowed(mode: Mode) -> bool:
    """Whether Execution Control may submit an order in ``mode``.

    REDUCE_ONLY remains submit-capable: its restriction is enforced at the
    approved-intent mint seam, while PAUSED and KILL_SWITCHED halt submission.
    """
    return mode not in _HALTED_MODES


class RiskPolicy(_Frozen):
    """A reviewed, versioned set of Decimal risk limits."""

    version: str = "v1"
    max_risk_per_trade: Decimal = Decimal("0.01")
    daily_realized_lockout_pct: Decimal = Decimal("0.03")
    # Deliberately wider than the realized-loss threshold until paper calibration.
    session_drawdown_pct: Decimal = Decimal("0.10")
    leverage_cap: Decimal = Decimal("1.5")
    stop_loss_required: bool = True


@dataclass(frozen=True)
class Settings:
    """Resolved control-plane settings for a run."""

    mode: Mode = Mode.PAPER
    paper_only: bool = True
    live_enabled: bool = False
    risk: RiskPolicy = field(default_factory=RiskPolicy)

    def effective_mode(self) -> Mode:
        """Return the mode after applying the paper-only safety guard."""
        return resolve_mode(self.mode, live_enabled=self.live_enabled and not self.paper_only)
