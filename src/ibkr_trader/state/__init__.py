"""state — SQLite persistence (PT-2): positions cache, daily realized P&L across restart, idempotency keys.

Daily realized P&L is persisted so the -3% loss lockout survives a restart (§6.1); idempotency keys make
order submission duplicate-safe. Empty until PT-2.
"""
