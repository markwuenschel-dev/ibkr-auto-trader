---
to: builder
from: reviewer
id: 032-pt-4b-accountsnapshot-ibkrsession
title: PT-4b — AccountSnapshot + IbkrSession extraction + generation fence
priority: normal
date: 2026-07-11
status: done
guardrails: [money, auth, data-integrity, safety]
depends_on: [029]
adr: docs/design/adr/0002-pt4-market-data-ingestion.md
---

## Summary

Rewrite PT-3's output to `AccountSnapshot` and extract the shared `IbkrSession`, per **ADR-0002 Decisions
3/9/10/12** and the **ADR-0001 Amendment ①/⑤/⑥/⑧/⑨** (lanes L3/L4). **Follow the ADRs exactly.** STAGED
DRAFT.

**Blocked by:** 029 (needs `HoldingValuation`/`ValuationStatus`/`InstrumentId`).

## Deliverables

- `src/ibkr_trader/ibkr/gateway.py`: `snapshot() -> AccountSnapshot`; `build_risk_context` →
  `build_account_snapshot`. `AccountSnapshot` = account fields (`net_liquidation`, `buying_power`,
  `maintenance_margin`) + `observed_at` + `HeldPosition[]`. The gateway **no longer constructs
  `RiskContext`**.
- `HeldPosition(instrument_id, symbol, quantity, broker_mark?, broker_market_value?, mark_available_at?,
  valuation_status)` — sourced from `ib.portfolio()` after the account-update subscription is **warmed +
  verified**, then **reconciled** portfolio-vs-position inventory. Held value uses `marketValue`, not
  `quantity × price`. `mark_available_at` = the `updatePortfolio` event receipt time (tracked in
  `IbkrSession`), not the account read time.
- `src/ibkr_trader/ibkr/session.py` (new): `IbkrSession` — **sole lifecycle owner** of one socket/clientId,
  reconnect events, health, `PacingGate`, serialized outbound-I/O lock, and a monotonically-incrementing
  **`generation`** bumped on reconnect. Started/stopped once by the composition root; neither adapter tears
  it down. Narrow scoped-request seam (acquire pacing → serialize → tag with generation), not a raw `IB()`.
- Rewire `IbkrAccountGateway` onto `IbkrSession`.
- `tests/test_ibkr_gateway.py` / `tests/test_ibkr_session.py`.

## The contract (ADR-0002 D3/9/10/12; ADR-0001 Amendment)

1. **`valuation_status = AVAILABLE | UNAVAILABLE`** — a typed state; the snapshot is *complete about
   inventory* while explicitly reporting valuation degradation. Never a hidden partial snapshot.
2. **Generation fence** — reconnect bumps `generation`; consumers reject a cycle that spans generations
   (prevents welding a pre-drop snapshot to post-reconnect data). Fail-closed → PT-13 handles flapping.
3. **PacingGate modeled, not hard-coded** — request class/cost + queue-delay/rejection telemetry; capture
   stays serialized (no `gather()`).
4. **ib_async stays quarantined** to `ibkr/*_ibkr*.py` + this session adapter; lazy import guard extended.

## Definition of done

- `snapshot()` returns `AccountSnapshot` with `HeldPosition[]`; `RiskContext` is never built here.
- Missing/unreconciled inventory → `SnapshotIncomplete` (never partial/defaulted).
- Held value uses `marketValue`; a short's signed value is preserved.
- A missing portfolio mark → `valuation_status = UNAVAILABLE`, never `0`.
- Reconnect bumps `generation`; a cross-generation read is rejected.
- Fake-based; one opt-in `@pytest.mark.integration` (`IBKR_INTEGRATION`) test excluded from CI.
- `ruff` + `pyright` green; full suite green.
