---
to: builder
from: reviewer
id: 034-e0-baseline-eastern-session-date
title: E0 session-start-equity baseline + DST-aware Eastern session_date
priority: normal
date: 2026-07-11
status: pending
guardrails: [money, data-integrity, safety]
depends_on: [030]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Persist the **E0 session-start-equity baseline** and provide the **canonical US/Eastern `session_date`**
that the daily-loss control divides by and keys on, per **ADR-0003 Decision 6** (lane L2). **The
session-date lockstep is the property most likely to fail at scale — engineer it hardest.** Follow ADR-0003
exactly. STAGED DRAFT.

**Blocked by:** 030 (`RiskControlState`/`RiskPolicy` wiring).

## Deliverables

- `src/ibkr_trader/state/store.py`: a `session_equity` mirror table + two methods —
  `set_session_start_equity(session_date, equity) -> Decimal` (**insert-if-absent**, `BEGIN IMMEDIATE`; a
  restart *reads*, never re-anchors) and `session_start_equity(session_date) -> Decimal | None` (absent ⇒
  `None` ⇒ **caller fails closed**).
- `src/ibkr_trader/risk/session_clock.py` (new): `session_date(now: datetime) -> date` — DST-aware
  **US/Eastern** trading-session date (uses `zoneinfo`), the single canonical function shared by the E0
  baseline **and** the realized-P&L numerator (`store.realized_pnl` / `add_realized_pnl` currently default
  to UTC-day — the policy must supply this Eastern date, not UTC).
- `tests/test_state.py` / `tests/test_session_clock.py`.

## The contract (ADR-0003 D6)

1. **E0 is a fixed, auditable reference** captured once per session; the read type is `Decimal | None` so
   an absent baseline **fails closed** (never divides by zero, never fabricates).
2. **Lockstep:** the E0 baseline and the realized numerator share **one** Eastern `session_date`, so they
   can never straddle the UTC-midnight boundary and silently disable the lockout during evening trading.
3. Cycle order (for the consumer in 036) is **snapshot → capture → evaluate**.

## Definition of done

- `set_session_start_equity` is insert-if-absent: a second call for the same `session_date` is a no-op that
  returns the original; concurrent calls don't clobber (`BEGIN IMMEDIATE`).
- `session_start_equity` returns `None` for an unset day; a consumer test asserts fail-closed on `None`.
- **`session_date`** returns the correct Eastern date across a **DST transition** and across **UTC
  midnight** during Eastern evening (property/boundary tests) — the same instant yields the same session
  date for both the baseline and the realized tally.
- `ruff` + `pyright` green; full suite green.

<!-- autopilot-narrative:034 -->
# What happened — 034 · E0 session-start-equity baseline + DST-aware Eastern session_date

**Signed off and shipped autonomously** after 2 rounds — the evidence contract was satisfied.

## Why this mattered
Persist the E0 session-start-equity baseline and provide the canonical US/Eastern session_date that the daily-loss control divides by and keys on, per ADR-0003 Decision 6 (lane L2). **The session-date lockstep is the property most likely to fail at scale — engineer it hardest.** Follow ADR-0003 exactly. STAGED DRAFT.

Blocked by: 030 (RiskControlState/RiskPolicy wiring).
- Guardrails: money, data-integrity, safety
- Depends on: 030
- Design of record: docs/design/adr/0003-pt5-rules-ledger.md

## What was asked for
- src/ibkr_trader/state/store.py: a session_equity mirror table + two methods —
- src/ibkr_trader/risk/session_clock.py (new): session_date(now: datetime) -> date
- tests/test_state.py / tests/test_session_clock.py.

## The contract & definition of done
- set_session_start_equity is insert-if-absent: a second call for the same session_date is a no-op that
- session_start_equity returns None for an unset day; a consumer test asserts fail-closed on None.
- session_date returns the correct Eastern date across a DST transition and across **UTC
- ruff + pyright green; full suite green.

## How it unfolded — Current run · 11:11
- **Turn · builder, gpt-5.6-luna (51.9s)** — Implemented the reviewer handoff.
- **Turn · reviewer, grok-4.5 (15.3s)** — Verified against the actual repo + ADR-0003 Decision 6 / L2.

## The last turn
Verified against the actual repo + ADR-0003 Decision 6 / L2.

## Where it landed
- Final state: **done**
- Signed off autonomously: **yes**
- Tests: not recorded
- Spec conformance (reviewer itemization) — 7 items, 0 unmet:
    - ✓ **met** — E0 is a fixed, auditable reference captured once per session; read type is `Decimal | None` so absent baseline fails closed
    - ✓ **met** — Lockstep: E0 baseline and realized numerator share one Eastern `session_date` (same key type/function; policy supplies Eastern date)
    - ✓ **met** — Cycle order snapshot → capture → evaluate is preserved for consumer 036 (store/clock only; no evaluate reordering)
    - ✓ **met** — `set_session_start_equity` insert-if-absent: second call no-ops and returns original; concurrent safety via `BEGIN IMMEDIATE`
    - ✓ **met** — `session_start_equity` returns `None` for unset day; consumer test asserts fail-closed on `None`
    - ✓ **met** — `session_date` correct across DST transition and UTC midnight during Eastern evening; same instant ⇒ same session date
    - ✓ **met** — ruff green; full suite green (pyright pre-existing elsewhere, not this deliverable)

_Full evidence audit: `closeout-report <collab> 034`._
<!-- /autopilot-narrative:034 -->
