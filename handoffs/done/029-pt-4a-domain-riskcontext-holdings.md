---
to: builder
from: reviewer
id: 029-pt-4a-domain-riskcontext-holdings
title: PT-4a — domain break — unified holdings RiskContext + conId identity + mint guard
priority: normal
date: 2026-07-11
status: done
guardrails: [data-integrity, money, safety]
depends_on: [PT-1 domain models, PT-2 state store]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement **PT-4a**, the breaking domain-model change every later PT-4/PT-5 handoff builds on. Design is
fully specified in **ADR-0003 Decision 4** (`docs/design/adr/0003-pt5-rules-ledger.md`) and **ADR-0002
Decisions 3/5/13** (lanes L1/L2) — this handoff is the implementation contract; **follow the ADRs
exactly**. This slice does *not* wire any consumer; it changes the frozen models + the positions cache so
the seams exist. `RiskContext` becomes the single sealed, mint-guarded, conId-keyed decision snapshot with
a **unified `holdings` map** (superseding ADR-0002's three parallel conId dicts).

**Blocked by:** — (root; all later handoffs depend on this).

## Deliverables

- `src/ibkr_trader/domain/models.py`:
  - `InstrumentId` (the broker `conId` set-key type) and `InstrumentRef` (`con_id`, display `symbol`,
    security type, exchange) + a resolver seam (`Protocol`) required before a target may enter the pipeline.
  - `ValuationStatus(StrEnum)` = `AVAILABLE | UNAVAILABLE`.
  - `HoldingValuation(_Frozen)` = `quantity: int`, `status: ValuationStatus`,
    `broker_market_value: Decimal | None`, `mark_available_at: datetime | None`.
  - `RiskContext` → `_MintGuarded` (new `ASSEMBLER_AUTHORITY`), carrying
    `holdings: Mapping[InstrumentId, HoldingValuation]`, `net_liquidation`, `buying_power`,
    `maintenance_margin`, `prices`, `price_basis`, `data_as_of` (all conId-keyed), `account_observed_at`,
    `as_of`, `context_digest`. **`positions` is a derived property over `holdings`, not a stored field.**
  - Add `ASSEMBLER_AUTHORITY = MintAuthority("decision-context-assembler")` — kept **off** the public
    package surface (do **not** re-export it from `domain/__init__.py`, unlike RISK/EXECUTION authorities).
  - Generalize the `_MintGuarded`/`MintAuthority` docstring + guard error text off "Risk & Sizing /
    Execution Control" to "only a designated issuer may construct this guarded fact."
- `src/ibkr_trader/state/store.py`: give the positions cache instrument identity — key by `InstrumentId`
  (conId) or add a `symbol ↔ conId` map. Migrate the existing symbol-keyed schema.
- `tests/test_domain_models.py` / `tests/test_state.py`: the DoD test surface below.

## The contract (ADR-0003 D4; ADR-0002 L1/L2)

1. **One map, `positions` derived.** `holdings` is the single authoritative per-instrument record; a
   separate authoritative `positions` map is forbidden (it invites key/quantity drift). `positions` is a
   computed view.
2. **Mint guard on `RiskContext`.** Direct `RiskContext(...)` raises; construction is only via
   `_mint(ASSEMBLER_AUTHORITY, …)`. The guard is **provenance, not security** — say so in the docstring;
   `model_construct()` and public-authority imports bypass it (closed later by the release gate, not here).
3. **Fail-closed valuation.** `AVAILABLE` requires non-null `broker_market_value` **and** UTC
   `mark_available_at ≤ as_of`; `UNAVAILABLE` carries no fabricated zero / quote fallback / last-value-forward.
4. **conId identity everywhere.** Positions, prices, `data_as_of`, `price_basis`, and `holdings` key on
   `InstrumentId`, never a ticker symbol. `InstrumentRef` carries display/routing metadata.
5. **No consumers wired here.** PT-3's `AccountSnapshot` (032), the assembler (033), and PT-5 (036) land
   later; this handoff only establishes the models + cache identity + mint seam.

## Definition of done — test surface (fake-based; CI-safe)

- Direct `RiskContext(...)` raises; `_mint(ASSEMBLER_AUTHORITY, …)` succeeds; a wrong/absent authority
  raises; `ASSEMBLER_AUTHORITY` is **not** importable from `domain/__init__`.
- `positions` derives exactly from `holdings` (signed quantities); no separate stored positions field.
- `AVAILABLE` with null `broker_market_value` or `mark_available_at > as_of` is rejected at construction.
- `UNAVAILABLE` holding never yields a fabricated `0`.
- Duplicate `InstrumentId` in `holdings` input is rejected.
- Positions cache round-trips by `InstrumentId`; a `symbol ↔ conId` lookup resolves; the migration from the
  old symbol-keyed schema preserves existing rows.
- `model_construct()` bypass is exercised and **documented as a known release-gate hole** (not fixed here).
- `ruff` + `pyright` green; full suite green.
