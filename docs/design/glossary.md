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
- **as_of / decision clock** — the injected UTC `Clock` value stamped on each snapshot. It *is* the clock
  the causal-data rule (PT-4) measures market-data timestamps against. Injected for test determinism;
  broker clock drift is surfaced via **clock skew** (`clock.skew` telemetry), never allowed to silently
  corrupt causal checks.
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
