"""The session-latched ``REDUCE_ONLY`` risk primitive.

This module is the sole definition of a reducing signed-position transition.
Producers latch a session here; the latch is passive and never submits an order
or auto-flattens a position.
"""

from __future__ import annotations

from datetime import date
from threading import RLock


class ReduceOnlyViolation(ValueError):
    """Raised when a latched session tries to mint a non-reducing order."""


def is_reducing(current_qty: int, resulting_qty: int) -> bool:
    """Return whether a signed position is strictly reduced without reversal."""
    if current_qty > 0 and resulting_qty < 0:
        return False
    if current_qty < 0 and resulting_qty > 0:
        return False
    return abs(resulting_qty) < abs(current_qty)


class ReduceOnlyLatch:
    """Thread-safe, explicitly managed set of reduce-only session dates.

    The caller owns the canonical (US/Eastern) session date.  There is no clock
    or expiry, so a restart or passage of time cannot silently clear a latch.
    """

    def __init__(self) -> None:
        self._sessions: set[date] = set()
        self._lock = RLock()

    def set(self, session_date: date) -> None:
        """Latch ``session_date`` into REDUCE_ONLY mode."""
        with self._lock:
            self._sessions.add(session_date)

    latch = set

    def clear(self, session_date: date) -> None:
        """Explicitly clear one session; absent dates are harmless."""
        with self._lock:
            self._sessions.discard(session_date)

    def is_latched(self, session_date: date) -> bool:
        """Return whether ``session_date`` is currently latched."""
        with self._lock:
            return session_date in self._sessions

    def permits(self, session_date: date, current_qty: int, resulting_qty: int) -> bool:
        """Return whether a transition is allowed in the supplied session."""
        return not self.is_latched(session_date) or is_reducing(current_qty, resulting_qty)

    def require_permitted(self, session_date: date, current_qty: int, resulting_qty: int) -> None:
        """Raise if a latched session's transition is not a strict reduction."""
        if not self.permits(session_date, current_qty, resulting_qty):
            raise ReduceOnlyViolation(
                "REDUCE_ONLY session permits only strict position reductions or full exits"
            )
