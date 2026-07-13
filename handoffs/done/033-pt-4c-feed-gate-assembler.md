---
to: builder
from: reviewer
id: 033-pt-4c-feed-gate-assembler
title: PT-4c — market feed + causal gate + DecisionContextAssembler
priority: normal
date: 2026-07-11
status: done
guardrails: [data-integrity, money, safety]
depends_on: [029, 032]
adr: docs/design/adr/0002-pt4-market-data-ingestion.md
---

## Summary

Implement the market-data feed, the pure causal gate, and the `DecisionContextAssembler` — the **sole
production constructor of `RiskContext`** — per **ADR-0002 Decisions 1/2/4/7/8/11/13/14** (lanes
L5/L6/L7/L9). **Follow ADR-0002 exactly**, but construct the **ADR-0003 D4 `holdings`-map `RiskContext`**
(not the superseded three-dict shape). STAGED DRAFT.

**Blocked by:** 029 (RiskContext/holdings + `ASSEMBLER_AUTHORITY`), 032 (`AccountSnapshot` + `IbkrSession`).

## Deliverables

- `src/ibkr_trader/ibkr/marketdata.py` (new, ib_async-free): `MarketDataFeed` `Protocol` +
  `_BaseMarketDataFeed` template + the pure causal-gate function. `QuoteBatch` of ordered
  `QuoteField(value, event_at?, available_at, basis)` per instrument.
- `src/ibkr_trader/ibkr/ibkr_marketdata.py`: `IbkrMarketDataFeed` (snapshot-pull; lazy import).
- `src/ibkr_trader/ibkr/fake_marketdata.py`: `FakeMarketDataFeed` (doubles as the replay feed; explicit
  historical `available_at`).
- `src/ibkr_trader/decision/assembler.py` (new): `DecisionContextAssembler.capture(requested) ->
  RiskContext` — serially acquire account snapshot then quote batch → **seal `decision_at`** (injected
  `decision_time_source`, never caller-supplied) → run the pure causal gate → assemble the sealed,
  mint-guarded `RiskContext` (`held ∪ requested` vs the **fresh** snapshot; mark precedence
  **broker Mark → last → close** with `price_basis`; seed held valuation from `AccountSnapshot`).
- `tests/test_marketdata.py` / `tests/test_assembler.py`.

## The contract (ADR-0002 D1/2/4/7/8/11/13/14)

1. **Seal at the close of collection.** `decision_at` stamped after account + quotes are gathered; a tick
   arriving during acquisition cannot cause a false look-ahead rejection.
2. **Causality keys on `available_at`**, strict, zero-tolerance (no epsilon). A field with no trustworthy
   availability metadata is **absent**, never guessed. Staleness (`event_at`) is a separate concern.
3. **Assembler is the sole `RiskContext` minter** (`_mint(ASSEMBLER_AUTHORITY, …)`). Serialized acquisition
   (no `gather()`). Generation fence honoured — a reconnect mid-capture rejects the cycle.
4. **Fail-closed valuation** — no causal datum ⇒ instrument absent from `prices`, `valuation_status =
   UNAVAILABLE` surfaced (never `0`, never last-value-forward). `SNAPSHOT_INCOMPLETE` ⇒ mint nothing.
5. **Build the `holdings`-map RiskContext** (ADR-0003 D4), `positions` derived.

## Definition of done — test surface (fakes; no live socket in CI)

- **Causal gate (hypothesis):** every accepted field has `available_at ≤ decision_at`; a later field is
  withheld + `md.lookahead`; **a post-open/pre-seal live tick IS accepted** (anti-starvation).
- **Replay determinism:** fixed series + advancing scheduled `decision_at` reveals data monotonically, zero
  look-ahead.
- **Future/non-UTC/misaligned** timestamp withheld even in live; generation-split cycle rejected.
- **Fail-closed absent:** no causal datum ⇒ absent (never `0`); `UNAVAILABLE` surfaced.
- **Mint guard:** direct `RiskContext(...)` raises; only the assembler + the test factory mint one.
- Port conformance; one opt-in integration test excluded from CI.
- `ruff` + `pyright` green; full suite green.
