# ADR 0003 — PT-5 Risk & Sizing / Rules Ledger

- **Status:** Accepted — design signed off 2026-07-11 (grilling session); implementation pending
  (handoffs 029–037, see `handoffs/MAP-pt4-pt5.md`).
  Revises the `RiskContext` shape from [ADR-0002](0002-pt4-market-data-ingestion.md) ③/⑬ (the unified
  `holdings` map supersedes the three conId-keyed dicts — recorded there and in Decision 4 below).
- **Slice:** PT-5 (the Rules Ledger + the Risk Approver that runs it) — with the PT-4a `RiskContext`
  contract it consumes, and cross-slice ripples into PT-6 (Planner/Projector), PT-7 (durable audit),
  PT-8 (`REDUCE_ONLY`), PT-9 (submission reservation), and PT-2 (E0 baseline store).
- **Depends on:** PT-1 (domain models, mint seam), PT-2 (SQLite state store — realized P&L, new E0
  baseline), PT-4a (sealed causal `RiskContext`). PT-6 supplies `RiskPlan`; PT-5 owns the approver.
- **Companion:** `trading-system-design.md` §2, §3, §7; `paper-trading-roadmap.md`; `glossary.md`
  (PT-5 section); ADR-0001, ADR-0002; `CONTEXT.md`.

## Context

PT-5 is the safety gate: it decides whether a `RiskPlan` may be minted into an `ApprovedOrderIntent`
that can cross into execution. The `trading-system-design.md` §3.3 table enumerated thirteen rules and
described the ledger as *"each rule is data… unit-testable in isolation."* The grilling reframed several
of its premises:

1. **The gate must not trust the planner.** A fat plan carrying the planner's own resulting-leverage /
   resulting-margin / notional projections, read as authority by the rules, is a train/serve-skew bug in
   disguise: the safety check would verify against the very numbers it is supposed to be checking. The
   approver **recomputes** every projection from canonical order terms and treats the planner's figures as
   *audit evidence only*.
2. **Control-plane facts are not decision-data facts.** `mode`, reviewed limits, the Eastern session date,
   and realized daily P&L have different ownership and freshness than the sealed causal snapshot. They do
   **not** belong in the PT-4 assembler's `RiskContext`; they live in a separate `RiskControlState`.
3. **The verdict space is binary.** All reduction/rounding/decline-to-plan is the planner's job; the
   approver either `APPROVED` or `REJECTED` a *fixed* plan. `PAUSED` is not an approver verdict — mode
   halts belong to Execution Control, durable operational pause belongs to PT-13.
4. **Several §3.3 rules relocate or are policy-blocked** (below). What remains is a tight ledger of
   pure risk-math + causal-integrity predicates.
5. **The "−3% daily loss limit" was two controls, not one** — a realized-loss opening gate (blind to
   unrealized bleed) and a total-equity drawdown breaker (blind to booked/stop-cascade bleed). Both are
   needed; both ship detection in v1 on a single shared session-start-equity baseline.

PT-5 reuses PT-1's mint seam (`_MintGuarded`/`MintAuthority`, `models.py:116`/`:34`), PT-2's durable
realized-P&L tally + atomic `BEGIN IMMEDIATE` write discipline (`store.py:134`), the injected-`Clock` /
skew machinery (ADR-0001 ⑨ as amended), the transient/fatal error split, and the §8 best-effort
telemetry envelope.

## Decisions

1. **Recompute-for-authority; fat plan is evidence, not authority (Option C).** The `RiskSizing.decide`
   facade stays single (`trading-system-design.md:75`); internally it is Planner → Approver over one deep,
   pure **`PortfolioProjector`** invoked **twice**: the planner uses it to *select* a candidate, the
   approver invokes it again on the **canonical order terms** to produce a **`VerifiedProjection`**. Rules
   read the `VerifiedProjection`, never the planner's claim. `RiskPlan` **may** carry a
   `planner_projection`, explicitly labelled **non-authoritative explanation**. A planner-vs-verified
   mismatch is a **hard `REJECTED` + alarm — never a silent repair.** Rollout is paper-only **shadow
   mode**: log the planner-vs-verified diff before the rule set enforces.

   ```
   RiskSizing.decide(intent, context, control_state) -> ApprovalDecision
     RiskPlanner.plan(intent, context, control_state)  # PortfolioProjector to select; may reduce/round/decline
     RiskApprover.approve(plan, context, control_state) # PortfolioProjector again on canonical terms
       -> RuleResult per rule over RiskEvaluation(plan, context, control_state, verified_projection)
       -> APPROVED (mint ApprovedOrderIntent) or REJECTED
   ```

2. **Binary verdict space; planner owns all reduction.** `ApprovalVerdict = APPROVED | REJECTED`. No
   `REDUCED`, no `REPLAN_REQUIRED`, no approver-computed quantity hint anywhere in PT-5–PT-7 — a reduced
   verdict *is* the approver repairing the plan, which Decision 1 forbids. §3.3 rules marked "reduce or
   reject" become **planner sizing constraints**; at the approver a failed blocking rule is `REJECTED`.
   The approver performs **no global-state mutation**: it returns severity + escalation evidence on
   rejection, and **PT-13** decides any durable pause. `PAUSED` is not a verdict — mode halts are
   ModeController/`submission_allowed` (`config.py:39`), and are honoured independently at execution.

   ```python
   class ApprovalVerdict(StrEnum):
       APPROVED = "APPROVED"
       REJECTED = "REJECTED"

   class ApprovalDecision(_Frozen):
       verdict: ApprovalVerdict
       plan_digest: str
       context_digest: str
       rule_results: tuple[RuleResult, ...]   # every rule evaluated — no short-circuit, for full audit
       audit_ref: str
       approved_order: ApprovedOrderIntent | None   # present only if APPROVED
   ```

3. **Two decision-input containers; `AccountSnapshot` never crosses the Risk & Sizing seam.** The approver
   receives exactly `RiskEvaluation(plan, context, control_state, verified_projection)`. `RiskContext` is
   the sealed causal snapshot (PT-4a, `ASSEMBLER_AUTHORITY`-minted); `RiskControlState` is the control
   plane (Decision 5). `AccountSnapshot` stays internal to the `DecisionContextAssembler` — everything PT-5
   needs about holdings is surfaced *on the sealed `RiskContext`* (Decision 4), so no `AccountSnapshot`
   leaks into the risk core.

4. **`RiskContext` (PT-4a freeze) carries a sealed per-holding valuation view — one map, `positions`
   derived (revises ADR-0002 ③/⑬).** ADR-0002 had `RiskContext` carry three parallel conId-keyed dicts
   (`positions`, `prices`, `data_as_of`) plus `price_basis`. Parallel maps invite key/quantity drift, and
   PT-5 needs per-holding valuation *health*, not just a price scalar (for reduce-only and for
   mark-preserving exposure math). Replace them with a single map:

   ```python
   class ValuationStatus(StrEnum):
       AVAILABLE = "AVAILABLE"
       UNAVAILABLE = "UNAVAILABLE"

   class HoldingValuation(_Frozen):
       quantity: int                        # signed broker inventory
       status: ValuationStatus
       broker_market_value: Decimal | None  # signed broker marketValue (preserves multipliers)
       mark_available_at: datetime | None   # UTC broker receipt time

   class RiskContext(_MintGuarded):         # ASSEMBLER_AUTHORITY
       holdings: Mapping[InstrumentId, HoldingValuation]
       # net_liquidation, buying_power, maintenance_margin,
       # prices, price_basis, data_as_of,
       # account_observed_at, as_of, context_digest, ...
   ```

   `positions` is **derived** from `holdings`, not separately authoritative. Fail-closed behaviour:
   `AVAILABLE` requires non-null `broker_market_value` and UTC `mark_available_at ≤ as_of`; `UNAVAILABLE`
   carries **no fabricated zero, no quote fallback, no last-value-forward**; an incomplete/unreconciled
   inventory means **`SNAPSHOT_INCOMPLETE` → mint no context, place no order**; a future/non-UTC/misaligned
   valuation timestamp is a **causal gate breach → alarm + pause automated decisions** (not a normal
   reject). `context_digest` covers each holding's quantity, status, market value, and mark receipt time.
   *This is a PT-4a change; it is sequenced ahead of PT-5 (Migration below).*

5. **`RiskControlState` + versioned Decimal `RiskPolicy`.** Control-plane facts move out of `RiskContext`.
   `mode` is **not** here (ModeController owns it; PT-5 may record the effective mode as audit evidence
   only). Reservation/idempotency state is **not** here (Execution Control owns it, Decision 11 relocation).

   ```python
   class RiskPolicy(_Frozen):               # replaces the float RiskLimits at config.py:45
       version: str
       max_risk_per_trade: Decimal          # 0.01
       daily_realized_lockout_pct: Decimal  # pct_a ≈ 0.03  (Control 1)
       session_drawdown_pct: Decimal        # pct_d — wider, PAPER-CALIBRATED tail stop (Control 2)
       leverage_cap: Decimal                # 1.5
       stop_loss_required: bool
       # concentration: ConcentrationPolicy — added only when fully defined (Decision 9 Open)

   class RiskControlState(_Frozen):
       policy: RiskPolicy
       session_date: date                   # canonical US/Eastern session (Decision 6)
       realized_daily_pnl: Decimal
       observed_at: datetime
   ```

   The `config.py:50-53` limits convert from `float` to `Decimal` before they participate in any money
   calculation, and gain a `version`. Both Planner and Approver read the same reviewed `RiskPolicy`;
   `policy.version` binds into the plan and the `ApprovalDecision` (Decision 12).

6. **The equity denominator: E0 for daily-loss; current NLV for leverage and max-risk.** Only
   `daily-loss-lockout` has a *live* denominator choice — `leverage-cap` on current NLV is **definitional**
   (a present-tense solvency ratio; a fixed baseline would make it margin-call-blind), and
   `max-risk-per-trade` on current NLV is **pre-decided spec** (its auto-de-risking as equity falls is
   intentional). Uniform-E0 is rejected. For daily-loss the base is **E0 = session-start equity**, chosen
   **not** because current NLV is "circular" (arithmetically current-NLV trips *slightly earlier*, not
   later) but because a **change-from-reference metric needs a fixed, auditable reference line**:
   realized-loss-over-*total*-equity is dimensionally incoherent and cannot be stated as a line in advance,
   and a control the protocol must "pause if breached" has to be *stateable*. **E0 is a hard contract:**
   captured **once** per session and persisted **insert-if-absent** (restarts *read*, never re-anchor);
   read as **`Decimal | None`** so an absent baseline **fails closed** rather than divides by zero; and
   keyed to a **single canonical US/Eastern `session_date`** shared with the realized numerator, so the two
   can never straddle the UTC-midnight boundary and silently disable the lockout during evening trading.
   **That session-date lockstep — not the denominator philosophy — is the property most likely to fail at
   scale, and is engineered hardest** (a DST-aware Eastern `session_date` function the Risk layer owes;
   PT-2 gains a mirror table + `set_session_start_equity` insert-if-absent / `session_start_equity ->
   Decimal | None`). Cycle order is **snapshot → capture → evaluate**.

7. **Two loss controls on the one shared E0; both detect in v1, one enforces dark.** The "−3% daily loss
   limit" is split into two genuinely complementary controls — one guards booked/stop-cascade bleed the
   other can't see, the other guards mark bleed the first is blind to:

   - **Control 1 — `daily-realized-opening-lockout` (active v1).** `realized_daily_pnl ≤
     −policy.daily_realized_lockout_pct · E0` ⇒ **`REDUCE_ONLY`**. Realized numerator, from the durable
     store; available today, no PT-4 price dependency.
   - **Control 2 — `session-drawdown-breaker` (detect + alarm active v1; enforcement dark).** `(NLV − E0) /
     E0 ≤ −policy.session_drawdown_pct` ⇒ **`REDUCE_ONLY` + alarm, escalate to PT-13** for durable pause.
     Its numerator is **broker `NetLiquidation`** — delivered by PT-3 today and, per ADR-0002 ⑫, *more*
     complete than any `Σ broker_market_value` (NLV also holds cash, FX, options, broker effects) — so it
     is **not** PT-4-gated. Its **enforcement** ships **dark** (detect + telemetry only) until PAPER
     telemetry sets `pct_d`; **an armed breaker is a LIVE prerequisite.**

   The breaker is **stateless-recompute** (restart-surviving without PT-13). Thresholds are **independent
   and unequally defaulted**: `pct_a ≈ 3%` realized is fine, but an equal-3% `pct_d` would **systematically
   fight the rebalancer** — forcing sell-into-weakness on exactly the dips a contrarian rebalance exists to
   buy — so `pct_d` must be a **wider, evidence-calibrated tail stop**. There is **no ordering invariant**
   between the two thresholds. `pct_d` calibration is the single most consequential open risk (Open below).

   **Three-rung action ladder, `REDUCE_ONLY` defined once at the mint seam.** All three producers — Control
   1, Control 2, and the unvalued-holding rule (Decision 8) — share one precise, mint-enforceable
   `REDUCE_ONLY` primitive: **`abs(resulting_qty) < abs(current_qty)` and no zero-crossing**; **latched per
   session; no auto-flatten.** A drawdown breach **de-risks, it never freezes** — freezing bets on reversal
   and traps open risk, contradicting the protocol's "never leave open risky positions." A harder
   `PAUSED`/`KILL_SWITCHED` that blocks even reductions is **reserved for a separate integrity/dislocation
   trigger**, mapped to the existing `Mode.PAUSED`/`KILL_SWITCHED` — never for drawdown.

8. **The v1 active-rule ledger (9 rules) + relocations + degraded/fallback rules.** Every active rule is a
   pure predicate over `RiskEvaluation`; none bricks paper operation and none silently approximates
   (an unconfigured rule **rejects-as-unconfigured**, it never guesses).

   | §3.3 rule | Disposition in PT-5 |
   |---|---|
   | `max-risk-per-trade` | **active** — `verified.max_loss_if_stopped ≤ policy.max_risk_per_trade · NLV` |
   | `daily-loss-lockout` | **active** — Control 1 (Decision 7); opening risk blocked, strict reductions/exits eligible |
   | `buying-power` | **active** — incremental BP debit ≤ `context.buying_power` (`models.py:69`); **not** naïve notional; no Open |
   | `leverage-cap` | **active** — `verified.resulting_gross_leverage < policy.leverage_cap`, gross = `Σ abs(broker_market_value)`; **fail-closed on any unpriced holding** (would otherwise fail *open* over empty prices) |
   | `stop-loss-required` | **active** — valid stop present, correct side |
   | `causal-data-only` (belt) | **active** — reject `data_as_of > as_of`, `account_observed_at > as_of`, missing/misaligned price↔basis↔time keys, non-UTC ts; a breach is a **gate breach → `REJECTED` + alarm**, not a normal reject |
   | `unvalued-holding reduce-only` | **active (NEW)** — `UNAVAILABLE` holding blocks opens/increases/zero-cross; strict reductions + full exits eligible (Decision 7 ladder) |
   | `concentration` | **active — target-weight fallback** (Decision 9) |
   | `maintenance-margin` | **active — degraded current-headroom** (Decision 10) |
   | `paper-first` | **RELOCATED → PT-8/PT-9** (`submission_allowed`); PT-5 records effective mode as audit evidence only |
   | `duplicate-prevention` | **RELOCATED → PT-9** (Decision 11); no advisory PT-5 predicate |
   | `no-direct-strategy-orders` | **RELEASE GATE, not a ledger predicate** (Decision 11) |
   | `taxable-account-guardrails` | **DEFERRED** — wash-sale/tax-lots not modelled at the paper milestone; §7 has no test |

   `audit-completeness` is **not** a ledger predicate: durable audit is PT-7/PT-12, and the approval **mint
   is contingent on a durable audit write succeeding** — a durable-audit-write failure mints nothing.

9. **`concentration` — target-weight fallback (active, exact, not an approximation).** The concentration
   ceiling defaults to the **strategy's declared target weight + drift band per instrument** (read from
   PT-11's declared targets). This is a *real* bound — you cannot hold more than target+drift of anything —
   just sourced from the strategy rather than a standalone policy, so it honours "never approximate"
   without bricking the rebalancer. **Owner:** PT-5 rule reading PT-11 targets. **Recorded dependency:** the
   fallback only binds *because the strategy declares weights*; a future strategy that emits intents
   **without** declared weights leaves the rule nothing to defer to, and it must then
   **reject-as-unconfigured**. **Resolution gate:** an explicit `ConcentrationPolicy` (absolute caps,
   sector/correlation grouping) supersedes the fallback — Open, **no LIVE gate** (the fallback is safe to
   run live as-is).

10. **`maintenance-margin` — degraded-but-functional now; buying-power is fully computable.** These are two
    distinct rules. **`buying-power`** needs nothing new (`context.buying_power` is required + fail-closed
    today) — **no Open**. **`maintenance-margin`** runs a **conservative current-headroom check now**:
    reject if current maintenance headroom minus the order's notional impact falls under a policy cushion.
    Parking it would ship v1 with *zero* margin protection while the inputs already sit in the context — a
    gratuitous gap. The honest caveat: a current-state check is only safe for **plain long equity**, where
    post-order margin is ~linear and boundable from current values + notional; for **shorts, options, or
    portfolio-margin offsets** margin impact is nonlinear, so the interim rule is **conservative
    (over-rejects, never under-rejects)** and scoped to what it can bound. **Owner:** PT-5 rule, PT-3
    inputs (already present). **Resolution gate:** the **IBKR what-if order-preview seam** makes it exact —
    **required before LIVE and before any short/options/complex instrument.**

11. **`duplicate-prevention` → PT-9; `no-direct-strategy-orders` → cross-cutting release gate.** Duplicate
    prevention is **fully PT-9**, with **no advisory PT-5 predicate**: PT-9 needs a **durable submission
    state machine** — not merely `record_order_key()` (`store.py:176`) — to survive a send-before-ack crash
    (`send` then crash before the ack ⇒ `UNKNOWN` ⇒ **reconcile before retry**). `no-direct-strategy-orders`
    is **removed from the ledger but is not "already structural"**: it is an **unsatisfied cross-cutting
    release gate** requiring process isolation for untrusted strategies, issuer containment, adapter
    runtime checks, and bypass tests — **a LIVE gate**, not a PT-5 v1 predicate.

12. **Mint-seam honesty vs the `model_construct` bypass.** The `_MintGuarded` seam proves *who assembled a
    guarded fact*, never *that it is correct* — it is a **provenance / accidental-bypass control, not a
    security boundary** against arbitrary in-process Python. Pydantic's `model_construct()` bypasses
    `__init__` (and thus the `MintAuthority` identity check), as do public-authority imports; both are
    **holes closed only by the Decision 11 release gate** (issuer containment + bypass tests), and the ADR
    prose and guard error text must say so plainly rather than imply the seam is a security control.
    Plans bind to `context_digest`, decision/session generation, and `policy.version`; mutable state is
    **revalidated at execution**, and the idempotency key is claimed **atomically with a risk reservation**
    before broker submission (PT-9).

## Design drivers (first-class)

- **Ultra-aggressive, continuously-held book.** The account runs an aggressive, continuously-invested
  posture — which is *why* unrealized bleed is not academic: a realized-only lockout would leave the book
  exposed to open-position drawdown it cannot see. This drives the two-control split (Decision 7).
- **Rebalancer-vs-breaker tension.** A contrarian rebalancer *buys the dips* the drawdown breaker would
  *sell into*. A too-tight `pct_d` makes the safety control fight the strategy's edge. Reconciling the two
  is a live calibration problem, not a constant — which is exactly why `pct_d` ships dark and
  paper-calibrated, and why its calibration (not the denominator or the action) is the load-bearing open
  risk.

## Telemetry (§8 envelope, from line one)

No PII. Decision provenance: `risk.decide` (verdict, plan/context digests, `policy.version`, correlation
id), per-rule `risk.rule` (id, pass/fail, severity, evidence). Projection integrity:
`risk.projection_mismatch` (planner-vs-verified diff — alarm). Loss controls: `risk.e0` (baseline
capture / read / absent-fail-closed), `risk.realized_lockout` (Control 1 trip), `risk.drawdown`
(Control 2 detect — value vs `pct_d`, dark/armed), `risk.reduce_only` (latch on/off, producer).
Integrity alarms: `risk.causal_breach`, `risk.snapshot_incomplete`, `risk.unpriced_holding`. Best-effort
emission never breaks the decision path.

## Definition of done — test surface (all via fakes; no live socket in CI)

- **Authority:** a max-risk / concentration / leverage violation returns `REJECTED`; **no second plan is
  generated**. A planner-reduced size is approved **only if the same fixed plan independently verifies**.
- **Projection mismatch:** planner-vs-verified divergence ⇒ `REJECTED` + `risk.projection_mismatch`; never
  a silent repair.
- **Decimal policy:** threshold-boundary tests on `Decimal` limits; no `float` in money arithmetic.
- **E0 hard contract:** capture-once insert-if-absent; a restart **reads** rather than re-anchors; an
  absent baseline (`Decimal | None`) **fails closed**, never divides by zero.
- **Session-date lockstep:** realized numerator and E0 baseline share one US/Eastern `session_date` across
  a UTC-midnight / DST boundary — no evening-trading window silently disables the lockout.
- **Control 1:** realized loss ≤ `−pct_a·E0` ⇒ `REDUCE_ONLY`; a strict risk-reducing exit still submits.
- **Control 2:** `(NLV−E0)/E0 ≤ −pct_d` ⇒ detect + alarm (dark) — and, once armed, `REDUCE_ONLY` +
  escalate; a drawdown breach **de-risks, never freezes**; recompute is restart-stable.
- **Reduce-only ladder (Decision 7 primitive):** an `UNAVAILABLE` holding (and a latched `REDUCE_ONLY`
  session) rejects any open/increase and any zero-crossing reduce; a strict reduce and a full exit pass.
- **Buying-power:** an **incremental BP debit**, not naïve notional, is compared against
  `context.buying_power`.
- **Maintenance-margin (degraded):** long-equity headroom check over-rejects at the uncertainty margin;
  short/options/complex remain **live-blocked** pending the what-if seam.
- **Concentration fallback:** bound = declared target+drift; a strategy intent **without** declared weights
  ⇒ **reject-as-unconfigured** (never approximated).
- **Causal belt:** `data_as_of`/`account_observed_at > as_of`, misaligned price↔basis↔time, or non-UTC ⇒
  `REJECTED` + `risk.causal_breach`.
- **Mint honesty:** a durable-audit-write failure mints **no** `ApprovedOrderIntent`; direct
  `ApprovedOrderIntent(...)` raises; **`model_construct()` and public-authority-import bypasses** are
  exercised and documented as release-gate holes.
- **Concurrency:** two individually-valid orders cannot **jointly** exceed a limit without the PT-9
  serialized reservation; a crash-after-send resolves to `UNKNOWN` and reconciles before retry.

## Consequences

- The gate never trusts the planner: authority is a projection **recomputed** from canonical terms, and the
  planner's numbers are auditable evidence. Rule tests stay trivial (each reads one verified scalar).
- v1 is an **honest two-control loss system** from day one — realized opening-gate *and* total-drawdown
  detection — because the breaker runs on NLV, not on marks. Only the breaker's durable human-cleared
  pause, and its *enforcement* threshold, wait (on PT-13 and on paper calibration respectively).
- One `REDUCE_ONLY` primitive, mint-enforced, serves three producers — no divergent definitions of
  "de-risk."
- Several controls are **degraded-but-honest** (concentration fallback, current-headroom margin) rather
  than parked — real protection now, with an explicit resolution gate and a LIVE prerequisite where the
  proxy stops being exact.
- The design's residual risk is **not** the denominator or the action — it is `pct_d` calibration on a
  book whose strategy actively opposes the breaker. That is carried as the top Open.

## Open (deferred — each recorded as a contract, not a TODO)

Each carries *(interim behaviour, owner, resolution gate)*:

- **`pct_d` calibration** — *interim:* Control 2 detect + alarm, enforcement **dark**; *owner:* PT-5 +
  paper telemetry; *gate:* PAPER-calibrated `pct_d` **before LIVE** (armed breaker is a LIVE prerequisite).
- **IBKR what-if margin seam** — *interim:* conservative long-equity current-headroom check (over-rejects);
  *owner:* PT-5 rule / PT-3 inputs; *gate:* what-if preview **before LIVE and before short/options/complex**.
- **Explicit `ConcentrationPolicy`** — *interim:* target-weight+drift fallback (active, exact while the
  strategy declares weights); *owner:* PT-5 / PT-11; *gate:* explicit policy supersedes the fallback (no
  LIVE gate; but reject-as-unconfigured if a future strategy omits weights).
- **Freshness thresholds (per price-basis)** — *interim:* data layer reports `md.stale`; no max-age block;
  *owner:* PT-5/PT-6; *gate:* explicit per-basis thresholds (a `CLOSE` basis is definitionally stale
  intraday) before a freshness rule may block.
- **`no-direct-strategy-orders` release gate** — *interim:* removed from ledger, not enforced; *owner:*
  cross-cutting (PT-8/PT-9/ops); *gate:* process isolation + issuer containment + adapter runtime checks +
  bypass tests (incl. `model_construct`) **before LIVE**.
- **PT-9 durable submission state machine** — *interim:* atomic `record_order_key` claim; *owner:* PT-9;
  *gate:* a durable send/ack/`UNKNOWN` reconcile state machine **before broker sends**.

## Mission — implementation lanes

PT-4a (the `RiskContext` holdings freeze, Decision 4) is a **breaking domain-model slice sequenced ahead of
PT-5** — order: amend docs (this ADR + glossary + design table + `CONTEXT.md`, done as part of this
this change) → convert `RiskLimits` to versioned `Decimal` `RiskPolicy` → add `REDUCE_ONLY` to PT-8 →
implement PT-4a holding valuation + PT-6 verified projections → implement PT-5 active rules, then PT-7
durable decision audit → implement PT-9 submission reservation/state before broker sends. PT-5's own lanes:

| Lane | Deliverable |
|---|---|
| L1 | **`RiskPolicy`**: `config.py:45` `RiskLimits` → frozen `Decimal` `RiskPolicy` with `version`, `daily_realized_lockout_pct`, `session_drawdown_pct`; both Planner + Approver read it. |
| L2 | **E0 baseline (PT-2 ripple)**: `session_equity` mirror table; `set_session_start_equity(session_date, equity)` insert-if-absent + `session_start_equity(session_date) -> Decimal \| None`; DST-aware US/Eastern `session_date()` shared with the realized tally. |
| L3 | **`RiskControlState` + `RiskEvaluation` + `RuleResult` + `ApprovalDecision`** types (binary verdict; digests; audit_ref). |
| L4 | **`PortfolioProjector`**: one deep pure module → `VerifiedProjection` (notional, incremental BP debit, resulting gross leverage over `Σ abs(broker_market_value)`, resulting margin headroom, resulting concentration, max-loss-if-stopped); fail-closed on any unpriced holding. |
| L5 | **`RiskPlanner`** (PT-6): owns reduction/rounding/decline; emits non-authoritative `planner_projection`; binds `context_digest`/generation/`policy.version`. |
| L6 | **`RiskApprover` + the 9-rule ledger**: each a pure predicate over `RiskEvaluation`; evaluate-all (no short-circuit) for full audit; `APPROVED` mint contingent on a durable audit write; planner-vs-verified mismatch ⇒ `REJECTED` + alarm. |
| L7 | **Two loss controls + `REDUCE_ONLY` ladder**: Control 1 gating + Control 2 detect/alarm (enforcement dark); one mint-seam `REDUCE_ONLY` primitive, session-latched, no auto-flatten; escalate Control 2 to PT-13. |
| L8 | **PT-8 `REDUCE_ONLY` mode primitive** (prerequisite for the daily-loss "exits eligible" half). |
| L9 | **Telemetry + audit evidence**: `risk.decide`/`risk.rule`/`risk.projection_mismatch`/`risk.e0`/`risk.drawdown`/`risk.reduce_only`/integrity alarms; PT-7 durable decision record (approve *and* reject). |
| L10 | **Adversarial properties**: projection mismatch, digest mismatch, stale/reconnected snapshot; unpriced/`UNAVAILABLE` holding → strict-reduce-only; zero/negative/non-finite prices; invalid stop direction; Decimal threshold edges; close-mark-as-fill; concurrent equivalent + distinct orders; Eastern/DST daily-P&L boundary; public-authority + `model_construct` bypass. |

**Definition of done:** the gate approves only a projection **recomputed** from canonical order terms over
sealed context + control state; no reduced/paused verdict and no approver-side sizing exists; the "−3%
daily loss limit" is an honest two-control system on a shared, restart-surviving, session-locked E0; every
degraded rule over-rejects rather than approximates; and no `ApprovedOrderIntent` is minted without a
durable audit record.
