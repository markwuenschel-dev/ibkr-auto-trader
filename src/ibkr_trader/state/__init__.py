"""state — SQLite persistence (PT-2): positions cache, daily realized P&L across restart, idempotency keys.

Daily realized P&L is persisted so the -3% loss lockout survives a restart (§6.1); idempotency keys make
order submission duplicate-safe. See :mod:`ibkr_trader.state.store`.
"""

from __future__ import annotations

from ibkr_trader.state.store import DB_PATH_ENV, DEFAULT_DB_PATH, StateStore

__all__ = ["DB_PATH_ENV", "DEFAULT_DB_PATH", "StateStore"]
