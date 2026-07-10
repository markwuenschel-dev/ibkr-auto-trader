---
to: builder
from: reviewer
id: 028-pt-3-ibkr-account-gateway
title: PT-3 IBKR connection/session → account snapshot → RiskContext
priority: normal
date: 2026-07-09
status: pending
guardrails: [money, auth, data-integrity, bounded-autonomy]
depends_on: [PT-1 domain models, PT-2 state store]
adr: docs/design/adr/0001-pt3-ibkr-account-gateway.md
---

## Summary

Implement PT-3: the IBKR connection/session layer that produces a frozen `RiskContext` (PT-1) account
snapshot. Design is fully specified and signed off in **ADR 0001**
(`docs/design/adr/0001-pt3-ibkr-account-gateway.md`) and `docs/design/glossary.md` — this handoff is the
implementation contract; follow the ADR exactly. ib_async + asyncio; account fields only
(`prices`/`data_as_of` are PT-4).

## Deliverables

- `src/ibkr_trader/ibkr/gateway.py` — the `AccountGateway` `Protocol` (`async connect() / snapshot() ->
  RiskContext / disconnect()`, `is_connected()`) and the `IbkrGatewayError` exception hierarchy.
- `src/ibkr_trader/ibkr/config.py` — frozen `IbkrConnectionConfig` (host, port, client_id, account,
  connect_timeout, readonly); env-sourced (`IBKR_HOST`/`IBKR_PORT`/`IBKR_CLIENT_ID`/`IBKR_ACCOUNT`); paper
  defaults `127.0.0.1:7497`.
- `src/ibkr_trader/ibkr/ibkr_gateway.py` — `IbkrAccountGateway` (the **only** module importing ib_async).
- `src/ibkr_trader/ibkr/fake_gateway.py` — `FakeAccountGateway` (in-memory, deterministic, no socket).
- `src/ibkr_trader/ibkr/__init__.py` — exports.
- `tests/test_ibkr_gateway.py` — the DoD test surface below (fake-based; CI-safe).
- `pyproject.toml` — ensure `ib_async` is a declared+installed dependency.

## The contract (ADR 0001, decisions ①–⑨)

1. **Port + real/fake seam.** Downstream depends on `AccountGateway`, never ib_async.
2. **One long-lived async gateway-owned session.** `connect()` once; `snapshot()` reads warm event-synced
   state; reconnect on drop. App (PT-13) owns the event loop; gateway owns one `IB()`/clientId.
3. **Read-only** (`readonly=True`), permanently; execution's writable session is PT-10.
4. **Account resolution:** configured account (assert in `getAccounts()`); else sole account; else **raise**
   `AccountResolutionError`. Under `PAPER`, assert `DU`-prefix else `PaperAssertionError`. Effective `Mode`
   is injected into the gateway.
5. **Reconcile broker vs PT-2 cache:** broker is truth; on divergence emit `positions.reconcile` + alert and
   rewrite the cache; return the reconciliation result as structured data. **Report, do not gate.**
6. **Resilience (reactive only):** bounded-backoff reconnect on `disconnectedEvent`/`errorEvent`
   (1100/1102/1300); `is_connected()`. No `Watchdog`/IBC (deferred). Active heartbeat is PT-13; pause is
   PT-8. Rate-limiting is a PT-4 seam.
7. **Errors — never default.** `IbkrGatewayError` root; *transient* (`NotConnected`, `SnapshotTimeout`,
   `SnapshotIncomplete`) retryable; *fatal* (`AccountResolutionError`, `PaperAssertionError`) raised at
   `connect()`, stops the run. A partial/zero-filled `RiskContext` is never returned.
8. **Money fields verbatim:** `NetLiquidation→net_liquidation`, `MaintMarginReq→maintenance_margin`,
   `BuyingPower→buying_power` (leveraged; PT-5 leverage cap constrains). `Decimal` from summary strings
   (never via `float`); positions signed `int` from `ib.positions(account)`.
9. **Clock:** `as_of` = injected UTC `Clock`; read `ib.reqCurrentTime()` and emit `clock.skew` past
   threshold. tz-aware UTC throughout.

## Telemetry (§8 envelope)

`ibkr.connect`, `ibkr.disconnect`, `ibkr.snapshot` (NLV/BP/margin/position-count — no account id/PII),
`positions.reconcile`, `clock.skew`.

## Definition of done — tests (all via `FakeAccountGateway`; no live socket in CI)

- Mapping: summary tags → `RiskContext`; Decimal-from-string; signed-int positions.
- Reconciliation: broker≠cache → `positions.reconcile` emitted + cache rewritten + no gating.
- Fail-closed: missing field → `SnapshotIncomplete`; unknown/ambiguous account → `AccountResolutionError`;
  non-`DU` under `PAPER` → `PaperAssertionError`; partial never returned.
- Resilience: `disconnectedEvent` → backoff reconnect → health recovers.
- Read-only: gateway configured `readonly=True`.
- One **opt-in** integration test vs a real paper Gateway, **excluded from CI** (marked, manual).
- `python -m pytest -q` green (fake-only, no Gateway).

## Out of scope (deferred — do NOT build here)

`prices`/`data_as_of` (PT-4), rate limiter (PT-4), `Watchdog`/IBC auto-restart (PT-13/ops), active
heartbeat (PT-13), pause-on-dirty-reconciliation (PT-8), writable execution session (PT-10).

<!-- autopilot-narrative:028 -->
# What happened — 028 · PT-3 IBKR connection/session → account snapshot → RiskContext

**Signed off and shipped autonomously** after 1 round — the evidence contract was satisfied.

## Why this mattered
Implement PT-3: the IBKR connection/session layer that produces a frozen RiskContext (PT-1) account snapshot. Design is fully specified and signed off in ADR 0001 (docs/design/adr/0001-pt3-ibkr-account-gateway.md) and docs/design/glossary.md — this handoff is the implementation contract; follow the ADR exactly. ib_async + asyncio; account fields only (prices/data_as_of are PT-4).
- Guardrails: money, auth, data-integrity, bounded-autonomy
- Depends on: PT-1 domain models, PT-2 state store
- Design of record: docs/design/adr/0001-pt3-ibkr-account-gateway.md

## What was asked for
- src/ibkr_trader/ibkr/gateway.py
- src/ibkr_trader/ibkr/config.py
- src/ibkr_trader/ibkr/ibkr_gateway.py
- src/ibkr_trader/ibkr/fake_gateway.py
- src/ibkr_trader/ibkr/__init__.py
- tests/test_ibkr_gateway.py
- pyproject.toml

## How it unfolded — run 20260710T194101 · started 2026-07-10T19:41:01Z
- **Round 1 · builder, gpt-5.6-terra (45.7s)** — Implemented the PT-3 dependency packaging requirement:

## The last turn
Implemented the PT-3 dependency packaging requirement:

## Where it landed
- Final state: **done**
- Signed off autonomously: **yes**
- Tests: passed

_Full evidence audit: `closeout-report <collab> 028`._
<!-- /autopilot-narrative:028 -->
