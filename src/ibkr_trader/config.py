"""config — the control plane's mode + reviewed risk policy. PAPER-first by construction.

The single most important invariant in the system lives here: **PAPER is the default, and LIVE is
rejected unless a reviewed config explicitly enables it** (trading-system-design.md §8 / PROTOCOL.md).
`PAUSED` and `KILL_SWITCHED` block *all* broker submission. Nothing here talks to a broker; it only
decides what mode we are in and supplies the account-level risk policy.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from ibkr_trader.domain.models import _Frozen


class Mode(enum.StrEnum):
    """Execution mode. The ModeController (PT-8) enforces transitions; this is the vocabulary."""

    PAPER = "PAPER"
    LIVE_SMALL_TEST = "LIVE_SMALL_TEST"
    LIVE = "LIVE"
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
    """Whether Execution Control may submit to an adapter in ``mode``."""
    return mode not in _HALTED_MODES


class RiskPolicy(_Frozen):
    """Reviewed, versioned Decimal limits consumed by Risk & Sizing.

    ``version`` is load-bearing: plans and approval decisions bind it to detect
    reviewed-policy staleness. Decimal defaults ensure that no binary float enters
    a limit comparison or money calculation.
    """

    version: str = "v1"
    max_risk_per_trade: Decimal = Decimal("0.01")
    daily_realized_lockout_pct: Decimal = Decimal("0.03")
    # TODO(pct_d): paper-calibrate. This deliberately wider tail-stop placeholder
    # must not be coupled to the realized-loss opening lockout threshold.
    session_drawdown_pct: Decimal = Decimal("0.10")
    leverage_cap: Decimal = Decimal("1.5")
    stop_loss_required: bool = True


@dataclass(frozen=True)
class Settings:
    """The resolved control-plane settings for a run."""

    mode: Mode = Mode.PAPER
    paper_only: bool = True  # master guard; must be flipped by reviewed config for live
    live_enabled: bool = False
    risk: RiskPolicy = field(default_factory=RiskPolicy)

    def effective_mode(self) -> Mode:
        return resolve_mode(self.mode, live_enabled=self.live_enabled and not self.paper_only)
