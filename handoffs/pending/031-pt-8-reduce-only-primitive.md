---
to: builder
from: reviewer
id: 031-pt-8-reduce-only-primitive
title: PT-8 REDUCE_ONLY mode + the mint-seam reduce-only primitive
priority: normal
date: 2026-07-11
status: pending
guardrails: [safety, bounded-autonomy, money]
depends_on: [029]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement the **one** `REDUCE_ONLY` primitive shared by all three producers (daily-loss lockout, drawdown
breaker, unvalued-holding rule), per **ADR-0003 Decision 7** (lane L8). This is the prerequisite for the
daily-loss "exits eligible" half. **Follow ADR-0003 exactly.** STAGED DRAFT.

**Blocked by:** 029 (mint seam + domain models).

## Deliverables

- `src/ibkr_trader/config.py`: `Mode` already has `PAUSED`/`KILL_SWITCHED`; add a **`REDUCE_ONLY`** operating
  state (a session-latched flag distinct from the hard halt modes — it de-risks, it does not freeze).
- `src/ibkr_trader/risk/reduce_only.py` (new): the single predicate
  `is_reducing(current_qty: int, resulting_qty: int) -> bool` = `abs(resulting) < abs(current)` **and no
  zero-crossing** (a long 100 "sold by 200" opens a short → blocked). Plus the **session latch** helper
  (set/clear per `session_date`, no auto-flatten).
- The mint-seam hook: the `ApprovedOrderIntent` mint path consults the latch — a latched `REDUCE_ONLY`
  session rejects any open/increase/zero-cross. (Interface only if the approver lands in 036; wire the
  check where the mint occurs.)
- `tests/test_reduce_only.py`.

## The contract (ADR-0003 D7)

1. **One definition, three producers.** No producer re-implements the reduce test.
2. **De-risk, never freeze.** `REDUCE_ONLY` permits strict reductions + full exits; it never blocks a
   risk-reducing order. A harder `PAUSED`/`KILL_SWITCHED` (blocks even reductions) is a **separate**
   integrity/dislocation trigger — not part of this handoff.
3. **Latched per session; no auto-flatten** — entering `REDUCE_ONLY` does not sell anything; it only
   constrains new orders until cleared.

## Definition of done

- `is_reducing`: strict reduce → True; full exit → True; open/increase → False; zero-crossing → False;
  sign-flip → False. Property test over signed quantities.
- Latch set for a `session_date` blocks opens/increases/zero-cross at the mint seam; clearing restores.
- No path auto-submits a flattening order.
- `ruff` + `pyright` green; full suite green.
