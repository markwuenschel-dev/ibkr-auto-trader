"""ibkr — connection/session (ib_async, asyncio), account snapshot -> RiskContext (PT-3; PT-4 adds prices).

The public surface is the ``AccountGateway`` *port* and its error model, plus the two implementations:
``FakeAccountGateway`` (in-memory, deterministic — what tests and the risk core depend on) and
``IbkrAccountGateway`` (the real ib_async adapter). Also home of resilience: reactive bounded-backoff
reconnection (§6.3); the active heartbeat/watchdog and pause live in PT-13/PT-8.

``IbkrAccountGateway`` is imported **lazily** (via ``__getattr__``) so that merely importing this package —
as the fake-based CI tests do — does not pull in ib_async. Only touching the real adapter imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import IbkrConnectionConfig
from .fake_gateway import FakeAccountGateway
from .fake_marketdata import FakeMarketDataFeed
from .gateway import (
    AccountGateway,
    AccountResolutionError,
    AccountSnapshot,
    Clock,
    FatalGatewayError,
    FixedClock,
    HeldPosition,
    IbkrGatewayError,
    NotConnected,
    PaperAssertionError,
    PositionReconciliation,
    SnapshotIncomplete,
    SnapshotTimeout,
    SystemClock,
    TransientGatewayError,
    build_account_snapshot,
    reconcile_positions,
    resolve_account,
)
from .marketdata import (
    MarketDataFeed,
    QuoteBatch,
    QuoteField,
    select_causal,
)
from .session import FakeSession, PacingGate, Session

if TYPE_CHECKING:  # for type-checkers only; runtime import is lazy (keeps ib_async out of CI)
    from .ibkr_gateway import IbkrAccountGateway
    from .ibkr_marketdata import IbkrMarketDataFeed

__all__ = [
    "AccountGateway",
    "AccountResolutionError",
    "AccountSnapshot",
    "Clock",
    "FakeAccountGateway",
    "FakeMarketDataFeed",
    "FakeSession",
    "FatalGatewayError",
    "FixedClock",
    "HeldPosition",
    "IbkrAccountGateway",
    "IbkrConnectionConfig",
    "IbkrGatewayError",
    "IbkrMarketDataFeed",
    "MarketDataFeed",
    "NotConnected",
    "PacingGate",
    "PaperAssertionError",
    "PositionReconciliation",
    "QuoteBatch",
    "QuoteField",
    "Session",
    "SnapshotIncomplete",
    "SnapshotTimeout",
    "SystemClock",
    "TransientGatewayError",
    "build_account_snapshot",
    "reconcile_positions",
    "resolve_account",
    "select_causal",
]


def __getattr__(name: str) -> object:
    """Lazily resolve the ib_async adapters so importing this package never requires ib_async (PEP 562)."""
    if name == "IbkrAccountGateway":
        from .ibkr_gateway import IbkrAccountGateway

        return IbkrAccountGateway
    if name == "IbkrMarketDataFeed":
        from .ibkr_marketdata import IbkrMarketDataFeed

        return IbkrMarketDataFeed
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
