# Ubiquitous Language — Glossary

Shared vocabulary for the trader. Terms are the words we use in code, handoffs, and ADRs. Seeded from the
PT-3 grilling (2026-07-09); grows per slice.

## PT-3 — IBKR connection / account snapshot

- **AccountGateway** — the domain *port* (a `Protocol`) that turns a broker connection into a
  `RiskContext`. Core method `async snapshot() -> RiskContext`, plus `connect`/`disconnect`/`is_connected`.
  The risk pipeline depends on this, never on ib_async. See [[ADR-0001]].
- **IbkrAccountGateway** — the real `AccountGateway` implementation, backed by ib_async. The *only* module
  that imports ib_async (the containment boundary).
- **FakeAccountGateway** — the in-memory, deterministic `AccountGateway` used by tests; no socket.
- **IbkrConnectionConfig** — frozen config in the `ibkr/` package: `host`, `port`, `client_id`, `account`,
  `connect_timeout`, `readonly`. Env-sourced; paper defaults (`127.0.0.1:7497`). Distinct from `config.py`
  (mode/risk control plane).
- **Account snapshot** — a single read of warm, event-synced broker state → a frozen `RiskContext`.
  Field mapping: `NetLiquidation`→`net_liquidation`, `BuyingPower`→`buying_power`,
  `MaintMarginReq`→`maintenance_margin`, `ib.positions()`→`positions` (signed shares by symbol).
- **buying_power (leveraged)** — IBKR `BuyingPower` is a RegT-leveraged figure (up to ~4× cash intraday).
  PT-3 reports it verbatim; the real over-sizing guard is PT-5's leverage cap (<1.5× gross) + 1%/trade, not
  the snapshot. Do not mistake it for deployable cash.
- **as_of / decision clock** — the injected UTC `Clock` value that is the causal cutoff a decision is made
  against. It *is* the clock the causal-data rule (PT-4) measures market-data timestamps against. Injected
  for test determinism; broker clock drift is surfaced via **clock skew** (`clock.skew` telemetry), never
  allowed to silently corrupt causal checks. **Amended by PT-4 (see `decision_at`):** the decision clock is
  stamped at the *close* of input collection and owned by the app-cycle coordinator, **not** by PT-3's
  account read — PT-3 records only its own `observed_at`. See [[ADR-0002]].
- **Read-only session** — the ib_async connection opened with `readonly=True`; it can read account summary
  and positions but the API refuses `placeOrder`. A structural order-block at a lower altitude than the
  `MintAuthority` seam.
- **Paper-account assertion** — under `Mode.PAPER`, the gateway asserts the resolved account is
  `DU`-prefixed (IBKR paper accounts) and fails closed otherwise.
- **Reconciliation** — the broker-vs-PT-2-cache position diff at snapshot time. Broker is truth; divergence
  emits `positions.reconcile` + alert and rewrites the cache. PT-3 *reports* it; it does not *gate* trading.
- **Reactive reconnection** — PT-3's connection resilience: bounded-backoff reconnect driven by observed
  `disconnectedEvent`/`errorEvent` (1100 lost / 1102 restored / 1300). Distinct from PT-13's **active
  heartbeat** (which catches a *wedged* gateway that never fires a disconnect).
- **IbkrGatewayError** — root of PT-3's exception hierarchy. *Transient* (`NotConnected`, `SnapshotTimeout`,
  `SnapshotIncomplete`) = retry while backoff runs. *Fatal* (`AccountResolutionError`,
  `PaperAssertionError`) = raised at `connect()`, stops the run. A snapshot never returns partial/defaulted
  data.

## PT-4 — Market-data ingestion (grilling in progress, 2026-07-10)

- **decision_at / decision seal** — the single UTC decision clock, stamped at the **close** of all
  decision-input collection (after the account read *and* the market-data batch), not at the open. Every
  input gathered before the seal is `ts ≤ decision_at` by construction, so an ordinary tick that prints
  *during* acquisition can never trigger a false look-ahead rejection. In replay it is the **scheduled**
  decision time, not wall-clock. Owned solely by the app-cycle coordinator. See [[ADR-0002]].
- **Causal gate** — a **pure function** that withholds any datum whose **`available_at > decision_at`**
  (see *availability time*), **strict, zero tolerance** (no epsilon). "Collected before the seal" does
  *not* prove availability is valid — a provider can report a genuinely future `available_at`, still
  rejected. Emits `md.lookahead`. An absolute causal invariant, distinct from freshness.
- **availability time (`available_at`) vs event time (`event_at`)** — the causal model keys on **when a
  datum became available to us**, not when the underlying market event happened. **Gate:**
  `available_at ≤ decision_at`. **Staleness:** evaluated on `event_at` *where available*. In **live**,
  `available_at` is the provider *receipt* time; in **replay**, historical availability is supplied
  explicitly. IBKR gives a real last-*trade* timestamp, but bid/ask, close, and the theoretical mark do
  **not** carry independent field timestamps in the `ib_async` ticker — and `close` is the *prior day's*
  adjusted close, whose `event_at` cannot be invented from snapshot-arrival time. **If a field has no
  trustworthy availability metadata, it is absent — never guessed.**
- **QuoteField(value, event_at?, available_at, basis)** — the raw per-field shape the feed reports in
  `QuoteBatch` (bid/ask/last/close/mark). `event_at` is optional (staleness only); `available_at` is
  mandatory (the gate). Raw fields are retained in `QuoteBatch`, **not** carried through `RiskContext`
  (yet).
- **Valuation mark + `price_basis`** — the single scalar the assembler writes to `RiskContext.prices`,
  chosen by precedence **broker Mark Price → last trade → close**, each *only with a known availability
  time*. IBKR's **Mark Price** is a theoretical TWS-P&L valuation (not executable) — the best *valuation*
  primary, though it does **not** guarantee `Σ(shares × mark)` reconciles to `NLV` (which also holds cash,
  FX, options, broker effects). **No `mid` fallback.** PT-1 gains one metadata field
  `price_basis[symbol] -> BROKER_MARK | LAST | CLOSE` (because `data_as_of` cannot encode both the
  availability time *and* the chosen field — the basis must be explicit and audited). `data_as_of[symbol]`
  now means the **availability time** used by the gate.
- **Valuation mark ≠ expected fill** — the scalar mark is for *valuation/sizing input*, never an assumed
  execution price. The 1%-risk cap is not a spread/impact model. PT-6 must later add a **side-aware
  execution-price rule** (ask for buys, bid for sells) + an explicit slippage policy, via a reviewed
  context extension — deferred (Open). Freshness is **basis-aware**: a `CLOSE` basis is definitionally
  stale intraday, and whether a close-marked symbol is tradeable is a PT-5/PT-6 policy call.
- **HeldPosition** — the enriched per-holding record inside `AccountSnapshot`:
  `HeldPosition(instrument_id, symbol, quantity, broker_mark?, broker_market_value?, mark_available_at?,
  valuation_status)`. Held **value arithmetic uses `broker_market_value`** (`ib_async` `marketValue`),
  **not** `quantity × price` — that preserves broker multipliers/valuation treatment. Sourced from
  `ib.portfolio()`, **but not by blindly swapping `positions()`→`portfolio()`**: the session must warm +
  **verify the account-update subscription**, then **reconcile portfolio inventory against position
  inventory** (a third reconciliation, alongside broker-vs-PT-2-cache). `valuation_status` is `VALUED` or
  `UNVALUED_HELD_POSITION`.
- **mark_available_at ≠ observed_at** — a held mark's availability is the **receipt time of its
  `updatePortfolio` event**, tracked per-event in `IbkrSession`, **not** the account read time. `ib_async`
  keeps portfolio as a cache populated by account updates, and IBKR portfolio/P&L updates *lag* (on a trade,
  or ~every 3 min) — so "when we read the cache" does not make a cached mark fresh. `valuation_status` is a
  **typed state** — `AVAILABLE | UNAVAILABLE` — so `AccountSnapshot` stays *complete about inventory* while
  explicitly reporting valuation degradation (never a hidden partial snapshot).
- **Instrument identity (`conId`, not symbol) & `InstrumentRef`** — the **set key** for the decision
  universe, positions, prices, and quotes is the broker **`conId`/instrument identity**, never the ticker
  symbol (symbols get reused/reassigned). Identity enters at the **strategy→decision seam**, not only
  inside `RiskContext`: define an **`InstrumentRef`** (≥ `con_id`, display `symbol`, security type,
  exchange) and require a **resolver** before a target may enter `capture()` — otherwise `StrategyIntent`,
  plans, and orders stay symbol-keyed while the context is conId-keyed. **PT-1 ripple:**
  `RiskContext.positions/prices/data_as_of/price_basis` become instrument-id-keyed; **PT-2 ripple:** the
  positions cache (symbol-keyed today) needs instrument identity or a `symbol↔conId` map.
- **Decision universe & ownership** — **Strategy** owns the *requested/target* universe; **AccountSnapshot**
  owns the *held* universe; the **DecisionContextAssembler** owns the *decision universe* = `held ∪
  requested`, unioned against the **fresh** broker snapshot it just captured (**not** a stale PT-2 cache);
  **PT-13** owns cadence and calls `capture(requested_universe)`. The market feed's set is the strategy's
  **requested execution universe**, *not* "non-held candidates" — the feed may **omit held symbols for
  valuation** (account marks seed those) but must still serve a held symbol the strategy intends to
  increase/reduce/exit (it needs a side-aware execution quote).
- **Unvalued-holding policy (reduce-only)** — a held position whose `valuation_status = UNAVAILABLE`
  **blocks all opening/increasing risk**, and permits only **strict reduce-only**:
  `abs(resulting_qty) < abs(current_qty)` **and no crossing through zero** (a long 100 "sold by 200" opens a
  short → blocked). Nothing fabricates a zero valuation; execution still applies its own quote/order
  safeguards before accepting even a reduce-only order. (Downstream policy for PT-5/6; PT-4 only reports
  `valuation_status` + `md.unavailable`.)
- **Freshness** — a *separate* safety rule from causality. Closing the clock prevents false look-ahead
  rejections; it does **not** make a read fresh. The data layer *records* per-source `observed_at` and
  collection duration and *reports* age (`md.stale`); the max-age **policy that can block a trade lives in
  PT-5/PT-6**, never in the data layer.
- **observed_at** — the per-source capture time (the account read; each quote's source `ts`), recorded for
  freshness *and* audit provenance. Distinct from `decision_at`: a context is never claimed to be observed
  later than its source was actually captured (this is why PT-4 does **not** `model_copy` PT-3's timestamp).
- **AccountSnapshot** — PT-3's *new* output type (the clean break): the account fields
  (`net_liquidation`, `buying_power`, `maintenance_margin`, `positions`) **+ `observed_at`**. It is *not* a
  decision input — it carries no `as_of` and no prices. `snapshot() -> AccountSnapshot`;
  `build_risk_context` is replaced by `build_account_snapshot`.
- **QuoteBatch** — the market feed's raw output: **ordered raw quotes per symbol** (live snapshots usually
  one; replay batches many), immutable, *unfiltered*. Ordering is load-bearing — it lets
  `causal_gate(batch, decision_at)` pick the latest causal quote per symbol deterministically.
- **DecisionContextAssembler** — a **deep module** (not a PT-13 convenience wrapper) and the **only
  production constructor of `RiskContext`**. `capture(symbols)` serially acquires the account snapshot then
  the quote batch, seals `decision_at`, runs the pure causal gate, and assembles the sealed `RiskContext`.
  PT-4 owns acquisition + sealing + selection + assembly; PT-13 owns cadence/lifecycle/retry/pause and
  *which* symbols to request.
- **decision_time_source** — the injected clock the assembler seals with: live/paper inject a real sealing
  clock; replay injects its scheduled simulation clock. `capture()` **never** takes a caller-supplied
  `decision_at` — that would let live code manufacture a convenient historical cutoff.
- **account_observed_at** — a field added to the sealed `RiskContext` so PT-5/PT-6 can enforce
  *account-data* freshness. `as_of` means "decision sealed," **not** "every account datum was observed
  then"; `account_observed_at` records when the account actually was.
- **Serialized acquisition** — the assembler acquires account then market data **sequentially, not via
  concurrent `gather()`**, because PT-3 and PT-4 may share one **paced IBKR session**; concurrency is
  withheld until session-sharing safety is explicitly proven (see the session fork, still open).
- **Guarded construction (generalized)** — `MintAuthority`/`_MintGuarded` (PT-1) is not "permission to
  execute"; it is "**only a designated issuer may construct this guarded domain fact.**" Three authorities,
  one mechanism: `ASSEMBLER_AUTHORITY` → `RiskContext` (sealed causal decision input), `RISK_AUTHORITY` →
  `ApprovedOrderIntent` (ledger-cleared intent), `EXECUTION_AUTHORITY` → `ExecutableOrder` (execution-
  authorized). The `models.py` docstring + guard error text must be generalized off "Risk & Sizing /
  Execution Control" only.
- **ASSEMBLER_AUTHORITY** — the authority that mints `RiskContext`. Kept **off** the public package surface
  (unlike `RISK_AUTHORITY`/`EXECUTION_AUTHORITY`, which are re-exported in `domain/__init__.py` today);
  injected only into the assembler + the test factory. The guard is a **provenance / accidental-bypass**
  control, **not** a security boundary against arbitrary in-process Python — untrusted strategy code would
  need *process isolation*, not Python-private names.
- **Two causal defenses (keep both)** — (1) **PT-4 gate**: select only `Quote.ts ≤ decision_at`.
  (2) **PT-5 belt**: reject any `data_as_of > as_of`, `account_observed_at > as_of`, missing/misaligned
  price-time keys, or non-UTC timestamp. The mint seam proves *who assembled* the context, never *that the
  assembly was correct* — so the belt is not optional. **Freshness** (max-age) is a *third*, distinct check.
- **IbkrSession** — the extracted **sole lifecycle owner** of one IBKR connection: socket, `clientId`,
  reconnect events, health, the pacing gate, and a serialized outbound-I/O lock. Started/stopped **once**
  by the composition root (PT-13); neither `IbkrAccountGateway` nor `IbkrMarketDataFeed` may independently
  tear it down. Both adapters share the one read-only session; execution gets its own writable session at
  PT-10. Exposes a **narrow scoped-request seam** (acquire pacing → serialize the op → tag it with the
  current generation), **not** a raw public `IB()` property and **not** a gateway reference handed to the
  feed. Connection + pacing locality live in this one deep module.
- **Session generation / generation fence** — a monotonically-incrementing counter `IbkrSession.generation`
  bumped on every reconnect. `capture()` records the generation before collection and **rejects the cycle**
  if either input — or the session at seal — belongs to a different generation. Prevents automatic
  reconnection from silently combining a *pre-drop* `AccountSnapshot` with *post-reconnect* quotes. A
  rejected cycle is fail-closed → PT-13's retry/pause policy handles the flapping-connection case (must not
  livelock).
- **PacingGate (`IbkrSession._pacing`)** — the pacing limiter, invoked before **every** outbound broker
  request. **Not** a hard-coded `50/s`: IBKR ties the allowed rate to market-data-line entitlement +
  configuration, and API pacing is per *client connection* while line limits span TWS+API. Model request
  **class/cost**, emit **queue-delay + rejection telemetry**, keep capture fully serialized until real
  streaming demand justifies measurement-backed concurrency. A future streaming feed = a *second*
  `IbkrSession` + clientId + its own gate, never a feed-to-gateway backchannel.
