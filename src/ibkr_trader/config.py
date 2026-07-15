"""Control-plane configuration for the paper-first trading system.

The mode vocabulary and reviewed risk policy live here.  This module does not
submit orders or talk to a broker; it supplies the inputs consumed by those
components.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ConfigDict

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
    """A reviewed, versioned set of Decimal risk limits.

    ``strict=True`` is load-bearing, not hygiene. ADR-0003 requires these limits to "convert from
    ``float`` to ``Decimal`` *before* they participate in any money calculation" (§Decision 5) and
    "no ``float`` in money arithmetic" (§Testing). Decimal *annotations* do not enforce that: pydantic's
    default lax mode coerces an inbound ``float`` via ``str()``, so a value carrying binary
    imprecision is preserved exactly rather than rejected --
    ``RiskPolicy(session_drawdown_pct=0.1 + 0.2)`` yielded ``Decimal("0.30000000000000004")``, and a
    session exactly 30% down then failed to trip Control 2. Lax mode likewise accepted
    ``stop_loss_required=0``, silently disabling the mandatory-stop flag.

    Strict REJECTS float ingress rather than normalising it: no rounding scale is specified anywhere
    in the ADR or this module, so quantising here would invent a limit the reviewer never approved.
    A policy is reviewed input -- callers pass ``Decimal`` (or a string via ``Decimal(...)``); there
    is no path where silently rounding someone's money limit is the safe reading.

    Scoped to this class, not to ``_Frozen``: its other subclasses ingest broker payloads whose
    documented parsing is Decimal-from-string, and widening strictness to them is a separate change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

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
