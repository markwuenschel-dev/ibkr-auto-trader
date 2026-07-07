"""config — the control plane's mode + risk settings. PAPER-first by construction.

The single most important invariant in the system lives here: **PAPER is the default, and LIVE is
rejected unless a reviewed config explicitly enables it** (trading-system-design.md §8 / PROTOCOL.md).
`PAUSED` and `KILL_SWITCHED` block *all* broker submission. Nothing here talks to a broker; it only
decides what mode we are in and what the account-level risk limits are. stdlib only.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


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
    """Resolve the effective mode. An unset request is PAPER. A LIVE request with ``live_enabled`` False
    is refused down to PAPER — live is opt-in via reviewed config, never a default (§8 invariant)."""
    if requested is None:
        return Mode.PAPER
    if requested in _LIVE_MODES and not live_enabled:
        return Mode.PAPER
    return requested


def submission_allowed(mode: Mode) -> bool:
    """Whether the ExecutionControl may hand an order to *any* adapter. False under kill/pause — the
    kill switch is enforced here, not deep in the adapter (PT-8/PT-9)."""
    return mode not in _HALTED_MODES


@dataclass(frozen=True)
class RiskLimits:
    """Account-level risk limits (the Rules Ledger, PT-5, enforces these per-order). Defaults are the
    PROTOCOL.md hard limits."""
    max_risk_per_trade: float = 0.01       # <= 1% of equity at risk per trade
    daily_loss_lockout: float = 0.03       # -3% realized daily P&L -> reject new opening risk
    leverage_cap: float = 1.5              # target gross leverage < 1.5x
    stop_loss_required: bool = True        # every order carries a stop or reviewed equivalent


@dataclass(frozen=True)
class Settings:
    """The resolved control-plane settings for a run."""
    mode: Mode = Mode.PAPER
    paper_only: bool = True                # master guard; must be flipped by reviewed config for live
    live_enabled: bool = False
    risk: RiskLimits = field(default_factory=RiskLimits)

    def effective_mode(self) -> Mode:
        return resolve_mode(self.mode, live_enabled=self.live_enabled and not self.paper_only)
