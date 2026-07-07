---
to: builder
from: reviewer
id: 027-pt-2-sqlite-state-store
title: PT-2 SQLite state store
priority: normal
date: 2026-07-07
status: pending
guardrails: [path-safety, data-integrity, bounded-autonomy, untrusted-agent-output]
---

## Summary

Implement PT-2: the SQLite state store for the paper-trading system.

Deliver `src/ibkr_trader/state/store.py` (wired via `src/ibkr_trader/state/__init__.py`):
- stdlib `sqlite3` only (no ORM); open at a configurable path (default `<repo>/state/trader.db`), WAL mode, schema created idempotently on init.
- positions cache: upsert + read signed share quantity per symbol.
- daily realized P&L that SURVIVES RESTART — persist realized pnl per UTC trading day so the -3% daily-loss lockout holds across a process restart (this is the point of the slice).
- idempotency keys: record + check submitted-order keys so a duplicate submission is a no-op.
- Decimal money stored as TEXT (exact), never float; all writes committed/atomic.
- No broker calls here (pure persistence); PAPER-first.

Tests (`tests/test_state.py`): positions upsert/read; daily P&L persists across reopening a new store on the same db file; idempotency dedupe; schema init is idempotent.

Definition of done: `uv run pytest -q`, `uv run ruff check`, and `pyright` all green.
