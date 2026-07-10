# ADR 0002 — PT-4 Market-Data Ingestion

- **Status:** Proposed — design sealed after a grilling session 2026-07-10; implementation pending. Amends
  [ADR-0001](0001-pt3-ibkr-account-gateway.md) ①/⑤/⑥/⑧/⑨ (see that file's *Amendment* section).
- **Slice:** PT-4 (market-data ingestion → the sealed, causal `RiskContext`).
- **Depends on:** PT-1 (domain models), PT-3 (`AccountGateway`, injected `Clock`, read-only session).
- **Companion:** `trading-system-design.md` §5, §6.4, §7; `paper-trading-roadmap.md`; `glossary.md` (PT-4
  section); ADR-0001.

## Context

PT-4 produces the market half of the decision snapshot and, with it, seals the whole `RiskContext` the risk
pipeline decides against. PT-1 reserved two fields no code fills yet (`domain/models.py:71-72`): `prices`
(symbol → price) and `data_as_of` (symbol → md timestamp for the causal check). The roadmap
(`paper-trading-roadmap.md:54`) defines PT-4 as *market-data ingestion; causal-only timestamps (≤ decision
clock)*, and the safety-invariant map fixes the rule: any datum later than the decision clock is rejected —
the `causal-data-only` rule (`trading-system-design.md:105`; invariant `:201`), the §6.4 "no train/serve
skew" guarantee (`:181`).

The grilling reframed the naïve "gate the feed against `as_of`" design, because three of its own decisions
collided in live: stamping the decision clock at the *open* of collection, then requiring all data to be
*older* than it, starves a live snapshot of the freshest tick (a real trade prints between the clock read
and the market-data fetch) — a liquidity-dependent false no-trade invisible to fake-based tests. The sealed
design below fixes that and the deeper issues it exposed (availability vs event time, provenance vs
causality, session/reconnect atomicity, held-position valuation, instrument identity). PT-4 therefore
necessarily reaches back into PT-1/PT-2/PT-3 — cheap now, since **no downstream slice consumes them yet**.

PT-4 reuses PT-3's machinery: the port + `_Base*` template + real/fake seam
(`AccountGateway`/`_BaseAccountGateway`, `gateway.py:254`/`:271`); tz-normalization (`_as_utc`, `:229`) and
skew (`clock_skew_seconds`, `:235`); the mint seam (`_MintGuarded`/`MintAuthority`, `models.py:116`/`:34`);
Decimal-from-string money parsing (`:150-180`); the transient/fatal error split (`:67-96`); best-effort
telemetry (`:43-47`, `:463`; `RecordingEmitter`, `test_ibkr_gateway.py:50`); and the lazy ib_async import
guard (`ibkr/__init__.py:63`).

## Decisions

1. **Seal the decision clock at the *close* of input collection (mirror/amends ADR-0001 ⑨).** A single
   `decision_at` is stamped **after** the account snapshot *and* the market-data batch are gathered, so every
   input is causal by construction and an ordinary tick arriving *during* acquisition cannot trigger a false
   look-ahead rejection. `decision_at` is owned by the `DecisionContextAssembler` via an injected
   `decision_time_source` — live/paper inject a real sealing clock, **replay injects its scheduled
   simulation clock**. `capture()` **never** accepts a caller-supplied `decision_at` (that would let live
   code manufacture a convenient historical cutoff). PT-3 keeps only its account `observed_at`.

2. **Causality keys on *availability* time, not event time.** The gate withholds any field with
   **`available_at > decision_at`**, strict, **zero tolerance** (no epsilon — an epsilon is a silent,
   arbitrary look-ahead allowance). "Collected before the seal" does *not* prove validity: a provider can
   report a genuinely future `available_at`, still rejected. **Staleness** is a *separate* concern evaluated
   on `event_at` where available. In live, `available_at` is provider *receipt* time; in replay it is
   supplied explicitly. IBKR gives a real last-*trade* timestamp, but bid/ask, close, and the theoretical
   mark carry no independent field timestamp in the `ib_async` ticker, and `close` is the *prior day's*
   adjusted close (its `event_at` cannot be invented from arrival time). **A field with no trustworthy
   availability metadata is absent — never guessed.**

3. **Clean type break — three types (amends ADR-0001 ①/⑧).** `AccountGateway.snapshot() -> AccountSnapshot`
   (account fields + `observed_at` + `HeldPosition[]`); `MarketDataFeed.quotes(requested) -> QuoteBatch`
   (raw, ordered per instrument); `DecisionContextAssembler.capture(requested) -> RiskContext`. The gateway
   no longer constructs `RiskContext`; `build_risk_context` becomes `build_account_snapshot`. No additive
   `RiskContext`, no `model_copy` of a timestamp (a context must never claim it was observed later than its
   sources actually were).

4. **`DecisionContextAssembler` is a deep module and the *sole* production constructor of `RiskContext`.**
   `capture(requested)` = serially acquire the account snapshot, then the quote batch → seal `decision_at`
   → run the pure causal gate → assemble the sealed `RiskContext`. PT-4 owns acquisition + sealing +
   raw-to-causal selection + assembly; **PT-13** owns cadence, lifecycle, retry/pause, and *which* symbols
   to request. Acquisition is **serialized (no `gather()`)** — PT-3 and PT-4 share one paced session;
   concurrency is withheld until session safety is measurement-proven.

5. **Guarded construction, generalized + `ASSEMBLER_AUTHORITY`.** `MintAuthority`/`_MintGuarded` are not
   "permission to execute" — they are "only a designated issuer may construct this guarded domain fact."
   `RiskContext` joins the seam (`ASSEMBLER_AUTHORITY`) alongside `ApprovedOrderIntent` (`RISK_AUTHORITY`)
   and `ExecutableOrder` (`EXECUTION_AUTHORITY`); the `models.py` docstring + guard error text are
   generalized off "Risk & Sizing / Execution Control." The mint seam is **provenance only** — it proves
   *who* assembled the context, never *that the assembly was correct*. `ASSEMBLER_AUTHORITY` is kept **off**
   the public package surface (unlike the two existing authorities, re-exported in `domain/__init__.py`
   today) and injected only into the assembler + a test factory; it is an accidental-bypass control, **not**
   a security boundary against arbitrary in-process Python (untrusted strategy code needs process isolation).

6. **Two causal defenses, both mandatory (mirror ③'s altitude framing).** (1) **PT-4 gate:** select only
   `available_at ≤ decision_at`. (2) **PT-5 belt** (`causal-data-only`): reject any `data_as_of > as_of`,
   `account_observed_at > as_of`, missing/misaligned price↔basis↔time keys, or non-UTC timestamp. The belt is
   the suspenders to the gate's belt (a buggy assembler holding the authority could still mint a non-causal
   context); if PT-5 ever observes `> as_of`, that is a **gate breach → alarm**, not a normal reject.

7. **Port + real/fake seam; ib_async quarantine restated.** A `MarketDataFeed` `Protocol` +
   `_BaseMarketDataFeed` template + the pure causal-gate logic live in a new **`ibkr/marketdata.py`**
   (ib_async-free); `IbkrMarketDataFeed` (`ibkr/ibkr_marketdata.py`) and `FakeMarketDataFeed`
   (`ibkr/fake_marketdata.py`, doubling as the replay feed) implement it. The invariant is reframed: **ib_async
   is quarantined to the `ibkr/*_ibkr*.py` adapters only** — port, base, gate, fakes, and every downstream
   slice stay ib_async-free (lazy `__getattr__` extended to `IbkrMarketDataFeed`).

8. **Snapshot-pull, not streaming (for now).** The rebalancer needs a point-in-time value per instrument per
   cycle, not a tick stream; use one-shot snapshot reads keyed to the cycle. One read → one seal keeps the
   causal reasoning trivial. A streaming/ticker-cache feed is deferred (Open) — and when it lands it is a
   *second* `IbkrSession` + clientId + pacing gate, never a feed-to-gateway backchannel.

9. **One shared read-only `IbkrSession` (extracted; amends ADR-0001 ⑥) with a generation fence.**
   `IbkrSession` is the **sole lifecycle owner** — socket, `clientId`, reconnect events, health, the
   `PacingGate`, and a serialized outbound-I/O lock — started/stopped **once** by the composition root;
   neither adapter may tear it down. **Generation fence:** reconnect increments `IbkrSession.generation`;
   `capture()` records the generation before collection and **rejects the cycle** if either input, or the
   session at seal, belongs to another generation — so automatic reconnection can never weld a *pre-drop*
   account snapshot to *post-reconnect* quotes. It exposes a narrow scoped-request seam (acquire pacing →
   serialize → tag with generation), not a raw `IB()` property.

10. **Pacing modeled, not hard-coded (`IbkrSession._pacing`).** Invoked before every outbound broker
    request. **Not** a fixed `50/s`: IBKR ties the rate to market-data-line entitlement + configuration, and
    API pacing is per client connection while line limits span TWS+API. Model request **class/cost**, emit
    **queue-delay + rejection** telemetry, keep capture serialized until real demand justifies measured
    concurrency.

11. **Valuation mark — broker Mark → last → close; `price_basis` audited; no `mid`.** The assembler writes a
    single scalar to `RiskContext.prices`, chosen by that precedence, each **only with a known
    `available_at`**. IBKR's **Mark Price** (theoretical TWS-P&L valuation, not executable) is the best
    *valuation* primary; it does **not** guarantee `Σ(shares × mark)` reconciles to `NLV` (which holds cash,
    FX, options, broker effects). Because `data_as_of` cannot encode both the availability time and the
    chosen field, PT-1 gains `price_basis[instrument] -> BROKER_MARK | LAST | CLOSE`. The mark is a
    **valuation/sizing input, never an assumed fill** — the 1%-risk cap is not a spread/impact model; a
    side-aware execution-price rule (ask for buys, bid for sells) + slippage policy is a PT-6 context
    extension (Open).

12. **Held-position valuation is account-sourced (amends ADR-0001 ⑤/⑧).** `AccountSnapshot` carries
    `HeldPosition(instrument_id, symbol, quantity, broker_mark?, broker_market_value?, mark_available_at?,
    valuation_status)`; held **value uses `broker_market_value`** (`ib_async` `marketValue`), not
    `quantity × price` (preserves broker multipliers). Sourced from `ib.portfolio()` — but **not** a blind
    `positions()`→`portfolio()` swap: the session warms + **verifies the account-update subscription**, then
    **reconciles portfolio inventory against position inventory**. `mark_available_at` is the receipt time of
    each `updatePortfolio` event (tracked in `IbkrSession`), **not** the account read time (IBKR
    portfolio/P&L updates lag — on a trade, or ~every 3 min). `valuation_status` is a **typed state**
    (`AVAILABLE | UNAVAILABLE`): the snapshot stays *complete about inventory* while explicitly reporting
    valuation degradation.

13. **Instrument identity is `conId`, entering at the strategy→decision seam.** The set key for the decision
    universe, positions, prices, and quotes is the broker **`conId`**, never a ticker symbol. Define an
    **`InstrumentRef`** (≥ `con_id`, display `symbol`, security type, exchange) and require a **resolver**
    before a target may enter `capture()`, so `StrategyIntent`/plans/orders do not stay symbol-keyed while
    the context is conId-keyed. **PT-1 ripple:** `RiskContext.positions/prices/data_as_of/price_basis`
    become instrument-id-keyed. **PT-2 ripple:** the positions cache needs instrument identity or a
    `symbol↔conId` map.

14. **Decision universe ownership + unvalued-holding policy.** **Strategy** owns the *requested* universe;
    **`AccountSnapshot`** owns the *held* universe; the **assembler** owns the *decision universe* =
    `held ∪ requested`, unioned against the **fresh** snapshot it just captured (never a stale PT-2 cache);
    **PT-13** calls `capture(requested)`. The feed's set is the strategy's **requested execution universe**
    — it may **omit held symbols for valuation** (account marks seed those) but must still serve a held
    symbol the strategy intends to increase/reduce/exit. Downstream (PT-5/6, recorded not built here): a
    `valuation_status = UNAVAILABLE` holding **blocks all opening/increasing risk** and permits only
    **strict reduce-only** (`abs(resulting) < abs(current)` **and no zero-crossing**). Nothing fabricates a
    zero valuation; PT-4 only reports `valuation_status` + `md.unavailable`.

15. **Naming reconciliation.** `RiskContext.prices` + `data_as_of` (+ new `price_basis`, `account_observed_at`)
    are **canonical**; the roadmap/glossary `market_data` wording is superseded (recorded here; the roadmap
    is not edited).

## Telemetry (§8 envelope, from line one)

No PII. Market: `md.snapshot` (instrument + stale counts), `md.lookahead` (`available_at > decision_at`
withheld; skew tie-in), `md.stale` (`event_at` age vs threshold), `md.unavailable`/`md.subscribe`. Cycle
provenance: `cycle_started_at`, `decision_at`, per-source `observed_at`/`available_at`, collection
durations. Session: `session.pacing` (queue delay / rejection), `session.generation` (reconnect bump),
`portfolio.reconcile` (portfolio-vs-position inventory). Best-effort emission never breaks the read path.

## Definition of done — test surface (all via fakes; no live socket in CI)

- **Causal gate (property, hypothesis):** ∀ field series, ∀ `decision_at`, every accepted field has
  `available_at ≤ decision_at`; a later field is withheld + `md.lookahead`; the instrument's value is
  unchanged. **A live tick arriving post-open/pre-seal is accepted** (the anti-starvation invariant).
- **Replay determinism:** a fixed series + monotonically advancing scheduled `decision_at` reveals data
  monotonically with zero look-ahead (§6.4 no-skew, exercised directly).
- **Future timestamps rejected:** a source reporting `available_at > decision_at` is withheld even in live.
- **Generation fence:** a reconnect mid-`capture()` (generation bump) → the cycle is **rejected**, never a
  spliced snapshot.
- **Fail-closed / absent:** no causal datum ⇒ instrument absent from `prices` (never `0`, never fabricated,
  never last-value-forward). `valuation_status = UNAVAILABLE` is surfaced, not hidden.
- **Staleness ≠ look-ahead:** `event_at` age > threshold ⇒ `md.stale` + flag, not withheld.
- **Mint guard:** direct `RiskContext(...)` raises; only the assembler (and the explicit test factory via
  `_mint(ASSEMBLER_AUTHORITY, …)`) constructs one.
- **Mapping:** price string → `Decimal`; `available_at`/`event_at` tz-aware UTC via `_as_utc`; `price_basis`
  matches the chosen field.
- **Assembly:** single sealed `as_of = decision_at`; `AccountSnapshot` account fields carried untouched;
  `held ∪ requested` unioned against the fresh snapshot.
- **Reduce-only (downstream contract):** an `UNAVAILABLE` holding rejects any open/increase and any
  zero-crossing reduce; a strict reduce and a full exit are allowed.
- **Port conformance:** `isinstance(feed, MarketDataFeed)`; **one opt-in** integration test vs a real paper
  Gateway (delayed `marketDataType`), `@pytest.mark.integration` + `IBKR_INTEGRATION`, **excluded from CI**.

## Consequences

- The snapshot is **causal by construction**; one feed + one sealed clock serve live/paper/backtest with no
  train/serve skew; the PT-5 belt turns any breach into a loud reject.
- ib_async stays quarantined to `*_ibkr*.py`; the risk core + backtest are unit-testable via fakes.
- Held valuation is account-authoritative and nearly always available; the dangerous "can't value my book"
  case shrinks to genuine feed gaps, handled by typed `UNAVAILABLE` + reduce-only.
- Delayed paper data makes **staleness normal**; downstream consumes `md.stale`, the data layer only reports.
- A flapping connection produces repeated fence rejections → **PT-13's retry/pause must not livelock** on it.
- The `AccountSnapshot`/`decision_at` bundle is *all-causal* but not a single instant (sources are seconds
  apart yet each ≤ `decision_at`) — internal-consistency tightening, if ever needed, is a future concern.

## Open (deferred to later slices)

- Streaming/ticker-cache feed (a second `IbkrSession`); `reqHistoricalData`/bar ingestion for the full §6.4
  backtest engine (only the causal-replay seam is fixed here).
- PT-6 side-aware **execution-price** rule + slippage policy (Decision 11).
- Concrete freshness thresholds + basis-aware tradeability policy (PT-5/6, Decision 12).
- Pacing tuning / pacing-violation backoff beyond the modeled seam (Decision 10).
- `RiskContext` internal-instant consistency (Consequences).

## Mission — implementation lanes

The grill widened this past the original 8 lanes; it now spans PT-1/PT-2/PT-3 as well as PT-4 (all cheap
now — nothing downstream consumes them yet). Docs (this ADR, the ADR-0001 amendment, the glossary PT-4
section) are already written as part of this Proposed change.

| Lane | Deliverable |
|---|---|
| L1 | **PT-1 domain**: `InstrumentRef` + resolver seam; conId-key `RiskContext.positions/prices/data_as_of`; add `price_basis`, `account_observed_at`; move `RiskContext` into `_MintGuarded` via `ASSEMBLER_AUTHORITY` (off public surface); generalize `MintAuthority`/`_MintGuarded` docstring + error text. |
| L2 | **PT-2 ripple**: instrument identity in the positions cache (conId, or a `symbol↔conId` map). |
| L3 | **PT-3 rewrite**: `AccountSnapshot` + `HeldPosition` from `ib.portfolio()` (subscription warm/verify + portfolio↔position reconcile; `marketValue`; `valuation_status`); `build_risk_context`→`build_account_snapshot`; `snapshot() -> AccountSnapshot`. |
| L4 | **`IbkrSession` extraction**: sole lifecycle owner; reconnect **generation fence**; `PacingGate` (class/cost model + queue/rejection telemetry); serialized I/O lock; rewire `IbkrAccountGateway` onto it. |
| L5 | **Market feed**: `MarketDataFeed` port + `_BaseMarketDataFeed` + `IbkrMarketDataFeed` + `FakeMarketDataFeed`; `QuoteBatch` of ordered `QuoteField(value, event_at?, available_at, basis)`; snapshot-pull; lazy import. |
| L6 | **Causal gate**: pure function `available_at ≤ decision_at`, latest-causal selection per instrument, `md.lookahead`. |
| L7 | **`DecisionContextAssembler`**: deep module, sole `RiskContext` constructor; `capture()` flow; injected `decision_time_source`; `held ∪ requested` vs fresh snapshot; mark precedence + `price_basis`; seed held valuation from `AccountSnapshot`. |
| L8 | **Telemetry/audit**: `cycle_started_at`, `decision_at`, per-source observed/available times + durations, `md.*`, `session.pacing`/`session.generation`, `portfolio.reconcile`. |
| L9 | **Fakes & replay**: `FakeMarketDataFeed` + replay feed with explicit historical availability; scheduled decision time vs wall-clock; fake session with reconnect/generation control. |
| L10 | **Adversarial properties**: post-open/pre-seal live tick accepted; future replay bar withheld; future availability rejected; fence splits a reconnect cycle; mint-guard direct construction raises; unvalued-holding strict reduce-only (no zero-cross); stale-but-causal handled by freshness. |

**Definition of done:** no production decision runs on input acquired after its seal; no datum with
`available_at > decision_at` crosses the gate; a normal tick arriving during acquisition cannot cause a
false no-trade; a reconnect can never splice a snapshot; and no held position is ever silently unvalued or
fabricated to zero.
