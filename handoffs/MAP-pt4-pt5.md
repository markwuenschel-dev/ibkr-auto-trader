# Wayfinder map — PT-4 (market data) + PT-5 (Rules Ledger)

`wayfinder:map` · charted 2026-07-11 · tracker = this repo's `handoffs/` system.

## Destination

A dependency-ordered set of reviewed handoffs that, built in id order by the autopilot, deliver
**PT-4a** (the breaking `RiskContext` → `holdings` model), the rest of **PT-4** (feed / causal gate /
assembler), and **PT-5** (Rules Ledger + Risk Approver) with the PT-6/7/8 ripples ADR-0002 + ADR-0003
require. Each handoff ships its own tests + adversarial lanes. **This map is a plan, not a build** — the
autopilot builds a ticket only once it is in `handoffs/pending/` and a run is started.

## Notes

- **The design is already sealed** in [ADR-0002](../docs/design/adr/0002-pt4-market-data-ingestion.md)
  (PT-4) and [ADR-0003](../docs/design/adr/0003-pt5-rules-ledger.md) (PT-5). Each handoff is an
  *implementation contract* — follow the ADR decisions/lanes exactly; do not re-litigate design.
- **Ordering is by handoff id, not `Blocked by:`.** The autopilot (`autopilot.py:666 _next_root`) drives
  the **lowest-id** `pending` handoff addressed to a CLI seat, one thread at a time, advancing **only on
  sign-off**. The `Blocked by:` lines are human-legible; the id sequence 029→037 is a valid topological
  sort, so lowest-id-first honours the dependencies.
- **Staging (chosen 2026-07-11):** **029 lives in `handoffs/pending/`**; **030–037 are staged in
  `handoffs/draft/`** (invisible to `_next_root`). Promote the next tranche into `pending/` after the prior
  signs off and you've eyeballed it. 029 is a breaking model change — keep it under your eye before the
  rest march.
- Design status: **ADR-0002 + ADR-0003 Accepted (signed off 2026-07-11).** The handoffs are binding
  implementation contracts — follow the ADRs exactly.

## Decisions so far (slicing choices)

- **PT-4a is the breaking root (029)** — `RiskContext` → unified `holdings` map + mint guard + conId
  identity; everything depends on it.
- **E0 baseline is its own handoff (034)**, not folded into 029's PT-2 ripple — it's PT-5-specific.
- **PortfolioProjector is separate (035)** from the PT-5 ledger (036) — a deep module both planner and
  approver use.
- **PT-7 audit is separate (037)** but 036's mint depends on it → 036 ships a stubbed durable-write
  interface that 037 implements.
- **PT-9 reservation is fog, not a frontier ticket** — nothing sends to a broker until PT-10.

## The handoff DAG

| ID | Handoff | ADR lanes | Blocked by | Location |
|---|---|---|---|---|
| 029 | PT-4a — domain break (`holdings`/mint/conId + PT-2 ripple) | 0002 L1/L2 + 0003 D4 | — | **pending/** |
| 030 | RiskPolicy (Decimal, versioned) + RiskControlState | 0003 D5, L1/L3 | 029 | draft/ |
| 031 | PT-8 `REDUCE_ONLY` mode + mint-seam reduce-only primitive | 0003 D7/L8 | 029 | draft/ |
| 032 | PT-4b — AccountSnapshot + IbkrSession + generation fence | 0002 L3/L4 | 029 | draft/ |
| 033 | PT-4c — market feed + causal gate + DecisionContextAssembler | 0002 L5/L6/L7/L9 | 029, 032 | draft/ |
| 034 | E0 session-start-equity store + Eastern `session_date` | 0003 D6/L2 | 030 | draft/ |
| 035 | PT-6 — RiskPlanner + PortfolioProjector → VerifiedProjection | 0003 D1/L4/L5 | 029, 030, 033 | draft/ |
| 036 | PT-5 — Risk Approver + 9-rule ledger + 2 loss controls | 0003 D2/7/8/9/10, L6/L7 | 030, 031, 034, 035 | draft/ |
| 037 | PT-7 — durable decision audit (approve *and* reject) | 0003 L9 | 036 | draft/ |

Shape: root **029** → data branch (032→033) ∥ control branch (030→031, 034) → converge at 035→036→037.

## Not yet specified (fog — ADR Opens; graduate later)

- **`pct_d` calibration** — the drawdown-breaker threshold; needs paper telemetry before enforcement arms
  (Control 2 ships detect+alarm only). Graduates after a paper run produces the telemetry.
- **IBKR what-if margin seam** — makes `maintenance-margin` exact for shorts/options/complex; a LIVE gate.
- **Explicit `ConcentrationPolicy`** — supersedes the target-weight fallback; no LIVE gate.
- **Freshness thresholds (per price-basis)** — before any freshness rule may block.
- **`no-direct-strategy-orders` release gate** — process isolation, issuer containment, `model_construct`
  bypass tests; a LIVE gate.

## Out of scope (this map)

- **PT-9 durable submission reservation / Execution Gate proper** — needed before broker sends (PT-10 era),
  not on PT-5's critical path. [ADR-0003 D11]
- **PT-10–15** — `paper_ibkr` adapter, `RebalancerStrategy`, broader audit log, app loop, Telegram, e2e.
