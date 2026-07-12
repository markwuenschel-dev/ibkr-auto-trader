"""session — the ib_async-free session seam (ADR-0002 ⑨/⑩).

``IbkrSession`` is the *sole lifecycle owner* of one broker connection: socket, ``clientId``, reconnect
events, health, the ``PacingGate``, a serialized outbound-I/O lock, a monotonically-incrementing
**generation** (bumped on reconnect), and the per-``conId`` ``updatePortfolio`` **receipt times**. The
real, socket-owning session lives in the ib_async adapter ``ibkr_session.py`` (imported lazily, never in
CI); *this* module holds only the parts that must stay ib_async-free — the ``PacingGate`` model, the
generation/receipt-time protocol, and ``FakeSession`` (which lets tests share one generation across a fake
gateway and a fake market feed to exercise the assembler's reconnect fence).

The quarantine (ADR-0002 ⑦): ib_async lives only in ``ibkr/*_ibkr*.py`` adapters + ``ibkr_session.py``;
port, base, gate, fakes, and every downstream slice stay ib_async-free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# Telemetry stage names (§8 envelope) — greppable constants.
STAGE_PACING = "session.pacing"
STAGE_GENERATION = "session.generation"
STAGE_PORTFOLIO_RECONCILE = "portfolio.reconcile"


@runtime_checkable
class Session(Protocol):
    """The narrow session seam the gateway + market feed depend on (never a raw ``IB()``).

    They read ``generation`` to tag each read; the assembler's fence rejects a cycle whose account
    snapshot and quotes disagree on it. ``bump_generation`` is driven by the reconnect path.
    """

    @property
    def generation(self) -> int: ...
    def bump_generation(self) -> None: ...


@dataclass
class PacingGate:
    """A modeled pacing seam (ADR-0002 ⑩) — request **class/cost**, not a fixed ``50/s``.

    IBKR ties the real rate to market-data-line entitlement + configuration, and API pacing is per client
    connection while line limits span TWS+API — so this is a seam to *measure and tune*, not a hard rule.
    It records each request's class/cost and exposes queue-delay/rejection counters for ``session.pacing``
    telemetry. Concurrency stays withheld (capture is serialized) until real demand justifies measured
    concurrency; the gate itself never blocks in this minimal form.
    """

    #: cost weight per request class; unknown classes cost 1. Tunable once real pacing is measured.
    class_cost: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    accumulated_cost: int = 0
    rejections: int = 0

    def charge(self, request_class: str) -> int:
        """Record one outbound request of ``request_class`` and return its cost."""
        cost = self.class_cost.get(request_class, 1)
        self.requests += 1
        self.accumulated_cost += cost
        return cost

    def snapshot(self) -> dict[str, int]:
        """The current pacing counters for a ``session.pacing`` event (no PII)."""
        return {
            "requests": self.requests,
            "accumulated_cost": self.accumulated_cost,
            "rejections": self.rejections,
        }


class _GenerationOwner:
    """Shared generation bookkeeping: a monotonic counter bumped on reconnect (ADR-0002 ⑨)."""

    def __init__(self, generation: int = 0) -> None:
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> None:
        self._generation += 1


class FakeSession(_GenerationOwner):
    """An in-memory ``Session`` for CI: shares one generation across a fake gateway + fake feed so the
    assembler's reconnect fence is exercised with no socket. Also carries a ``PacingGate`` + I/O lock so
    the serialized-acquisition path is the same code the real session runs.
    """

    def __init__(self, generation: int = 0, *, pacing: PacingGate | None = None) -> None:
        super().__init__(generation)
        self.pacing = pacing or PacingGate()
        self._io_lock = asyncio.Lock()

    async def run_serialized(self, coro_factory: Callable[[], Awaitable[object]]) -> object:
        """Serialize one outbound read under the I/O lock (no ``gather()`` — ADR-0002 ④/⑨)."""
        async with self._io_lock:
            return await coro_factory()

    # Receipt-time tracking is a real-session (socket) concern; the fake sets marks explicitly on its
    # HeldPosition fixtures, so there is nothing to track here.
    def portfolio_mark_at(self, con_id: int) -> datetime | None:  # pragma: no cover - fake has none
        return None


def reconcile_inventory(
    portfolio_qty: dict[int, int], position_qty: dict[int, int]
) -> tuple[bool, list[int]]:
    """Reconcile portfolio-sourced inventory against position-sourced inventory by conId (ADR-0002 ⑫).

    Returns ``(ok, mismatched_con_ids)``. Non-zero quantities only. A mismatch means the account-update
    subscription is not trustworthy for valuation yet → the caller fails the snapshot closed rather than
    valuing a book it cannot verify.
    """
    keys = {k for k, q in portfolio_qty.items() if q != 0} | {
        k for k, q in position_qty.items() if q != 0
    }
    mismatched = sorted(k for k in keys if portfolio_qty.get(k, 0) != position_qty.get(k, 0))
    return (not mismatched, mismatched)


def aggregate_signed(rows: Iterable[tuple[int, int]]) -> dict[int, int]:
    """Sum signed quantities by conId across possibly-duplicate broker rows."""
    out: dict[int, int] = {}
    for con_id, qty in rows:
        out[int(con_id)] = out.get(int(con_id), 0) + int(qty)
    return out
