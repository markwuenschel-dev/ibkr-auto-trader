# ADR 0001 — PT-3 IBKR Account Gateway

- **Status:** Accepted — design signed off after a grilling session 2026-07-09; implementation pending (handoff 028).
- **Slice:** PT-3 (IBKR connection/session → account snapshot → `RiskContext`).
- **Depends on:** PT-1 (domain models, `RiskContext`), PT-2 (SQLite state store, positions cache).
- **Companion:** `trading-system-design.md` §6.3, §8; `paper-trading-roadmap.md`.

## Context

PT-3 turns a live IBKR connection into a frozen `RiskContext` (PT-1) that the risk pipeline decides
against. `RiskContext` fields PT-3 owns: `as_of`, `net_liquidation`, `buying_power`,
`maintenance_margin`, `positions` (`prices`/`data_as_of` are PT-4). The system is test-first and
paper-first; a live IB Gateway is external I/O that can't be unit-tested directly. ib_async provides
`connectAsync(host, port, clientId, timeout, readonly, account)`, `reqAccountSummaryAsync()` (tags incl.
`NetLiquidation`, `BuyingPower`, `MaintMarginReq`), `ib.positions(account)` (signed `.position` per
`.contract.symbol`), a `Watchdog`/IBC reconnection rig, and read-only connections that refuse
`placeOrder`.

## Decisions

1. **Port + real/fake seam.** Define an `AccountGateway` `Protocol` (`async connect/snapshot/disconnect`,
   `snapshot() -> RiskContext`). Ship `IbkrAccountGateway` (ib_async, the *only* file importing ib_async)
   and `FakeAccountGateway` (in-memory, deterministic, for tests). Downstream slices depend on the port,
   never on ib_async. Mirrors the existing `ExecutionAdapter` protocol pattern.
2. **One long-lived, gateway-owned async session.** `connect()` once at startup; `snapshot()` reads the
   warm, event-synced account/position state; the gateway reconnects on drop. The app loop (PT-13) owns
   the asyncio event loop; the gateway owns the single `IB()`/clientId for the process lifetime. Not
   connect-per-snapshot.
3. **Read-only data session** (`readonly=True`), kept read-only permanently; execution (PT-10) opens its
   own writable session later (two clientIds). Defense-in-depth *at a lower altitude than* the
   `MintAuthority` seam — read-only still returns account summary + positions, so it costs nothing now.
4. **Explicit account, fail-closed on ambiguity + paper assertion.** Use the configured account (assert it
   is in `getAccounts()`); if unset and exactly one account exists, use it; if unset and multiple,
   **raise**. When effective `Mode` is `PAPER`, assert the account is `DU`-prefixed else fail closed. The
   `DU` guard lives *inside* the gateway (effective `Mode` injected). Connection settings live in a frozen
   `IbkrConnectionConfig` in the `ibkr/` package (env-sourced; paper defaults `127.0.0.1:7497`);
   `config.py` stays the mode/risk control plane.
5. **Positions reconciliation — report, don't gate.** Broker is source of truth for the snapshot. PT-3
   diffs broker vs the PT-2 cache, emits a `positions.reconcile` telemetry event + alert on divergence,
   and writes broker-truth back into the cache (a restart-survival mirror, never an override). PT-3
   **reports** the discrepancy as structured data; the decision to pause opening risk belongs to PT-8/
   PT-13.
6. **Resilience split by altitude; roll our own reconnection.** PT-3 owns *reactive* connection resilience
   (connect-with-bounded-backoff; `disconnectedEvent`/`errorEvent` codes 1100/1102/1300; reconnect loop;
   `is_connected()` health; `ibkr.connect`/`ibkr.disconnect` telemetry). PT-13 owns the *active* heartbeat
   (wedged-gateway case), PT-8 owns pause. **Do not adopt `Watchdog`/IBC** (it manages the Gateway
   *process* and probes via market-data) — recorded as a deferred ops option. Rate-limiting is a PT-4 seam.
7. **Error model — never default, split transient/fatal.** `snapshot()` never returns a partial or
   zero-filled `RiskContext`; a missing required field raises. Exceptions root at `IbkrGatewayError`:
   *transient* (`NotConnected`, `SnapshotTimeout`, `SnapshotIncomplete`) → caller may retry while backoff
   runs; *fatal* (`AccountResolutionError`, `PaperAssertionError`) → raised at `connect()`, stops the run
   (no retry-loop against a misconfiguration). PT-13 catches and decides pause/halt; PT-3 only raises.
8. **Money-field mapping — broker's figures, verbatim.** `NetLiquidation → net_liquidation`,
   `MaintMarginReq → maintenance_margin` (current requirement, not `FullMaintMarginReq`),
   `BuyingPower → buying_power` **verbatim** (a RegT-leveraged, up to ~4× intraday figure — the over-sizing
   guard is PT-5's leverage cap <1.5× + 1%/trade, *not* the data layer). Summary strings parse straight to
   `Decimal` (never via `float`); positions are signed `int`. PT-3 reports broker numbers; it does not bake
   risk policy into the snapshot.
9. **Clock — injected UTC, skew-monitored.** `as_of` is an injected `Clock` (`now()` → tz-aware UTC),
   deterministic in tests. Broker time (`ib.reqCurrentTime()`) is read and compared; drift past a threshold
   emits `clock.skew`. `as_of` *is* the decision clock the causal-data rule (PT-4) measures market-data
   timestamps against — we own it and monitor drift rather than surrender it to the broker's clock.

## Telemetry (§8 envelope, from line one)

`ibkr.connect`, `ibkr.disconnect`, `ibkr.snapshot` (NLV/BP/margin/position-count — no account id/PII),
`positions.reconcile`, `clock.skew`.

## Definition of done — test surface (all via `FakeAccountGateway`; no live socket in CI)

- Mapping: summary tags → `RiskContext`; Decimal-from-string; signed-int positions.
- Reconciliation: broker≠cache → `positions.reconcile` emitted + cache rewritten + **no gating**.
- Fail-closed: missing field → `SnapshotIncomplete`; unknown/ambiguous account → `AccountResolutionError`;
  non-`DU` under `PAPER` → `PaperAssertionError`; a partial `RiskContext` is never returned.
- Resilience: `disconnectedEvent` → bounded-backoff reconnect → health recovers.
- Read-only: gateway configured `readonly=True`.
- One **opt-in** integration test against a real paper Gateway, **excluded from CI** (marked, manual).
- Dependency: `ib_async` installed for the real adapter to import; CI is fake-only and stays green without a
  Gateway.

## Consequences

- ib_async is quarantined to one adapter; the risk core is unit-testable via `FakeAccountGateway`.
- Two clientIds eventually (data + execution); a paper run cannot read a live account or place an order.
- A dirty reconciliation is loud and structured but non-blocking here — PT-8/PT-13 must consume it.
- A wedged (not dropped) gateway is *not* caught by PT-3; PT-13's active heartbeat must cover it.

## Open (deferred to later slices)

- `Watchdog`/IBC auto-Gateway-restart (PT-13/ops). Shared rate-limiter (PT-4). Writable execution session
  (PT-10). Pause-on-dirty-reconciliation policy (PT-8).
