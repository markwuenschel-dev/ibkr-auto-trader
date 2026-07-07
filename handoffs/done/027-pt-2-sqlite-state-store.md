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

## Reviewer decisions (resolves builder round-1 questions — build directly, do NOT reply with notes)

1. **DB path — APPROVED.** Repo-anchored default `<repo>/state/trader.db` with an `IBKR_TRADER_STATE_DB` env override. Do NOT copy telemetry.py's CWD-relative default: a CWD-keyed lockout ledger silently resets on restart-from-another-dir, killing the -3% lockout exactly when it matters.
2. **Daily-P&L ownership — the STORE owns the sum.** Implement `add_realized_pnl(amount, *, day=None) -> Decimal` (accumulate, lost-update-safe) + `realized_pnl(day=None)`. The store is the restart-surviving source of truth; the Risk layer (PT-5/PT-7) may snapshot later but does not own persistence.
3. **UTC trading day — keep the store policy-free.** Default `day` to `datetime.now(UTC).date()` per spec, but keep `day` injectable. The US/Eastern session-boundary policy is DEFERRED to the Risk layer, which will pass the canonical session date. Do not embed session logic in the store.
4. **Idempotency — claim-on-write.** `record_order_key(key) -> bool` returns True only on first insert (`INSERT OR IGNORE` + rowcount), atomic. Document that order submission MUST branch on this bool; `has_order_key` is read-only convenience, not the dedupe primitive (check-then-act is a TOCTOU race).
5. **Durability — `synchronous=FULL` + WAL, APPROVED.** Single locked connection, explicit `BEGIN IMMEDIATE` around the pnl read-modify-write.
6. **Additions:** add a `.gitignore` rule for `state/*.db*` (WAL `-wal`/`-shm` sidecars); keep `all_positions() -> dict[str, int]`. SKIP `PRAGMA user_version`/migrations and retention pruning this slice (YAGNI; a later PT).

**Build instruction:** WRITE `src/ibkr_trader/state/store.py`, wire exports through `src/ibkr_trader/state/__init__.py`, add `tests/test_state.py` (including the restart-survival test on a real db file), add the `.gitignore` rule — then run the DoD and report actual output.
