# Paper-Trading Roadmap

> How many handoffs stand between today and a working paper-trading loop, in dependency order.
> Companion to `trading-system-design.md` (the spec) and `collab-kit-architecture*.md` (the tooling).
> Written 2026-07-06.

## Where we are

_Status updated 2026-07-17. The original 2026-07-06 snapshot described a pre-code workspace and is now
false — this repo has since landed ~8 PT slices; the corrected state follows._

- **collab-kit (orchestration): transplanted into this repo, green.** The engine now lives here under
  `collab/tools/lib/` — not a separate `collab-kit` repo. Foundation slices 001–006 and autonomy slices
  012–015 are in `handoffs/done/`; the full suite is green under `scripts/verify.py` (the command CI runs).
- **The trading system: PT-1/2/3/4a–c/6 + the reduce-only primitive have landed.** `ibkr-auto-trader` is
  a git repository with a package (`pyproject.toml`). Built: frozen domain models (PT-1); SQLite state
  store (PT-2, handoff 027); IBKR account gateway + session (PT-3, handoff 028); domain
  `RiskContext`/holdings, `AccountSnapshot`, and the feed-gate assembler (PT-4a–c, handoffs 029/032/033);
  `RiskPolicy`/`RiskControlState` and the E0/Eastern-session baseline (handoffs 030/034); the reduce-only
  primitive (handoff 031); and `RiskPlanner` + `PortfolioProjector` (PT-6, `42f0661`, refined in handoff
  035). **Still stubs** (`Empty until PT-N` in code): PT-5 (approver + loss-control *enforcement*), PT-8
  (execution control), PT-12 (durable audit).
- **Board:** `pending/` empty; handoff **035** (PT-6) is in flight; `draft/` holds 036 (PT-5) and 037 (PT-7).
- **Nothing trades live.** Paper-first is the only path; no broker submission exists yet.
- **Optional, off the critical path:** collab-kit §13 slice 8 (`install.sh` / `newproject` / `restart` /
  `/collab` skill). Nice for distribution, but not required — this repo already exists as a workspace.

## Paper-trading definition of done

From `trading-system-design.md` §8. A full loop where:

1. `RebalancerStrategy` emits `StrategyIntent`s from target allocation + drift thresholds;
2. **Risk & Sizing** sizes them, then approves/rejects under the **Rules Ledger** (1%/trade, −3% daily
   lockout, buying-power, margin, leverage < 1.5x, mandatory stop, causal-data-only, idempotency);
3. **Execution Control** authorizes in **PAPER** mode and routes to the `simulated` (default) or
   `paper_ibkr` adapter — never `live_ibkr`;
4. **every decision is audited** (approve *and* reject; structured JSON + human line);
5. a **kill switch / PAUSE** halts all broker submission;
6. **Telegram** surfaces drift, paper fills, daily summary, and errors.

Structural safety (must hold throughout): strategy APIs emit only `StrategyIntent`; trusted execution
must enforce the `no-direct-strategy-orders` release gate before broker submission. Mint seams are
provenance / accidental-bypass controls, not a hard in-process security boundary; adapters accept
**only** `ExecutableOrder` as their ordinary API contract. **PAPER is the default** and LIVE is rejected
unless enabled by reviewed config + approved workflow state.

## The handoff roadmap — 16 slices (PT-0 … PT-15)

Each slice = one reviewed handoff shipping its own tests (per the collab convention). `PT-*` labels are
decoupled from handoff ids (which will be 027+). Guardrail tags drive whether the adversarial
regression-hunt lanes run.

| Slice | Scope | Guardrails | Depends on |
|---|---|---|---|
| **PT-0** | **Project bootstrap** — `git init`; `uv` + `pyproject.toml` (Python 3.14); package skeleton (`domain/ ibkr/ strategy/ risk/ execution/ state/ audit/ app.py tests/`); `structlog`; `pytest` + CI; `MODE=PAPER` default config plumbing | infra | — |
| **PT-1** | **Domain models** — frozen pydantic: `RiskContext`, `StrategyIntent`, `RiskPlan`, `ApprovedOrderIntent`, `ExecutableOrder`, `Fill`/`Ack`; provenance seams for the trusted Risk & Sizing / Execution Control pipeline | data-integrity, money | PT-0 |
| **PT-2** | **SQLite state store** — positions cache, daily realized P&L that survives restart, idempotency keys | data-integrity, money | PT-1 |
| **PT-3** | **IBKR connection/session** — `ib_async` + asyncio; account snapshot (NLV, positions, buying power, margin) → `RiskContext` | money, auth | PT-1, PT-2 |
| **PT-4** | **Market-data ingestion** — feeds `RiskContext.market_data`; causal-only timestamps (≤ decision clock) | data-integrity | PT-3 |
| **PT-5** | **Rules Ledger** — account/risk rules: max-risk 1%/trade, −3% daily-loss lockout, buying-power, maintenance-margin, leverage < 1.5x, stop-loss-required, causal-data-only, duplicate-prevention | money, safety | PT-1, PT-2 |
| **PT-6** | **Risk Planner** — sizes intents into a `RiskPlan` carrying a stop | money | PT-4, PT-5 |
| **PT-7** | **Risk Approver** — evaluates the plan against the Rules Ledger, logs approve *and* reject, mints `ApprovedOrderIntent` | money, safety | PT-5, PT-6 |
| **PT-8** | **Mode Controller + kill switch/pause** — PAPER default; LIVE rejected under default config; `KILL_SWITCHED`/`PAUSED` block all submission | safety, bounded-autonomy | PT-1 |
| **PT-9** | **Execution Gate + adapters (protocol + `simulated`)** — mints `ExecutableOrder`; `ExecutionAdapter` protocol; in-process simulated adapter (the default) returns `Fill`/`Ack` | safety, money | PT-7, PT-8 |
| **PT-10** | **`paper_ibkr` adapter** — routes `ExecutableOrder`s to an IBKR paper account | money, safety | PT-3, PT-9 |
| **PT-11** | **`RebalancerStrategy`** — target allocation + drift thresholds; emits `StrategyIntent` only. **Owns the real `StrategyIntent` shape:** it declares only `symbol`/`target_weight`/`rationale` today, but `RiskPlanner.plan` reads ~10 fields off the intent (`instrument_id`, `side`, `stop_price`, `price`, `quantity`, `lot_size`, `multiplier`, …) via duck-typing — so `plan(intent: Any)` stays deliberately untyped until PT-11 defines the contract (deferred **INT-006b**, from INT-006; do not invent it earlier). | money | PT-4 |
| **PT-12** | **Audit log** — structured JSON + human-readable line for every decision (approve & reject) | audit-completeness, data-integrity | PT-1 |
| **PT-13** | **App main loop** — orchestration + heartbeat/watchdog + reconnection/rate-limiting; safe pause on stall/disconnect | safety, bounded-autonomy | PT-3, PT-9, PT-12 |
| **PT-14** | **Telegram alerts** — via the collab-kit bridge: large drift, paper order placed, daily summary, errors | observability | PT-13 |
| **PT-15** | **End-to-end paper loop + invariant sweep** — wire the full pipeline; property-based/integration tests mapping the 13 safety invariants (§7); run 24/7 in paper | safety, data-integrity | all |

**Count: 16 handoffs.** PT-5 (13 rules) may split into account-risk vs. tax/concentration rules
(concentration cap, taxable-account guardrails, audit-completeness, no-direct-strategy-orders provenance)
→ **17**.

### Deferred past the paper milestone (not counted)
- ML `SignalStrategy` implementation (only the seam is built during PT-11).
- Backtest / walk-forward harness (design exists; not in the §8 paper list).
- `live_ibkr` adapter and the LIVE / LIVE_SMALL_TEST mode transitions (FUTURE-ONLY, gated).
- Partial-fill / order-status modeling depth; small-account odd-lot handling (open questions).

## Guardrail reminders (hold across every slice)

- **Paper-first / paper-default**; LIVE rejected unless enabled by reviewed config + approved workflow.
- **Kill switch / PAUSE** prevents any broker submission.
- **Provenance/type seams**: strategy APIs emit `StrategyIntent`; trusted execution enforces the release
  gate; adapters accept `ExecutableOrder` as their ordinary API contract.
- **Every decision audited**, including rejections; **same Risk & Sizing** across backtest/paper/live (no
  train/serve skew); **causal data only**; **human veto is final**.
- Money/safety-touching slices additionally trigger the adversarial regression-hunt lanes at review.

## Tech (locked)

Python 3.14 + `uv`; `ib_async` (async IBKR); `pydantic` (frozen models); `pandas`/`numpy` (strict causal);
`structlog`; SQLite; `pytest` + property-based risk tests. (The stdlib-only constraint applies to
collab-kit, **not** the trading system.)

## Reusable-Core overlay (the bigger frame)

This build is also **test run #1 for the Reusable Core** (`Reusable_Core_Domain-Agnostic_Agent_Loop`) —
the trader is its first *coding domain pack* (§12). Two decisions apply to every slice:

- **Instrument to the §8 telemetry envelope from line one** (`src/ibkr_trader/telemetry.py`). Every
  gate/decision/handoff is a JSONL event, so the run is legible (fixing the "couldn't track the work"
  pain) *and* accumulates the labeled trace corpus the calibrated risk layer (§6/§11) will later train on.
- **Fail-closed now, calibrated-waiver later.** The trader has a sound execution oracle, so per §5.7 the
  existing fail-closed collab-kit substrate is *sufficient* — we do **not** build the risk aggregator /
  conformal certification yet. It gets added after traces + labels exist, per §11's "log first" order.

## Status

- **PT-0 — DONE (2026-07-06).** `git init`; `pyproject.toml` (Python 3.14, deps declared not installed);
  `src/ibkr_trader/` skeleton (domain/ibkr/strategy/risk/execution/state/audit); PAPER-default control
  plane (`config.py`); the §8 telemetry emitter (`telemetry.py`); the §12 pack declaration (`pack.py`,
  `oracle=execution`); `app.bootstrap`. `python -m pytest` → 13 passed (stdlib-only).
- **Next: PT-1** (domain models). Build directly on this substrate, or seed it as handoff `027` and drive
  it through the autonomous loop — the scaffold + telemetry are now in place for either.
