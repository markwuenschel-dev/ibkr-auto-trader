---
to: reviewer
from: builder
id: 001-initial-design
priority: high
title: "Initial Architecture & Safety Design for IBKR Automated Trading System"
date: 2026-07-03
---

## Summary
Propose a clean, modular, auditable architecture for an automated trading system targeting the user's ~$2k taxable IBKR account. The system will start exclusively in paper trading and follow the PROTOCOL.md strictly.

## Key Design Principles
- **Safety-first layered architecture**:
  1. **Core Engine** — manages connection to IBKR, account state polling, order execution with full guards.
  2. **Risk & Sizing Module** — single source of truth for position sizing, daily loss limits, margin checks. Every order must pass through here.
  3. **Strategy Layer** — pluggable strategies (initially simple rebalancer or momentum filter on existing holdings). Strategies only output *desired target weights* or *signals* — never place orders directly.
  4. **Decision Logger & Auditor** — writes structured JSON + human-readable logs for every decision.
  5. **Control Plane** — config flags (PAPER_ONLY=True by default), kill switch, Telegram alerts.

- **No direct order placement from strategy code**.
- **State machine** for trading mode: PAPER | LIVE_SMALL_TEST | LIVE (with explicit transitions only after reviews).
- **Heartbeat + watchdog** — if main loop stalls or connection lost, system pauses safely and alerts.
- **Backtesting harness** that re-uses the exact same Risk & Sizing + Strategy modules (to avoid train/serve skew).

## Proposed Tech Choices
- Python 3.14+
- pnpm
- `uv`
- `ib_insync` (or `ib_async`) for clean async IBKR API access
- `pydantic` for config and structured logs
- `pandas` + `numpy` for analysis (with strict causal handling)
- `pytest` + property-based testing for risk module
- Structured logging (structlog or similar) + daily decision audit files
- Simple file-based or SQLite for persistent state (positions, daily P&L, last decision time)

## First Milestone (Paper Trading Only)
1. Robust connection + account snapshot (NetLiquidation, positions, buying power, margin).
2. Basic rebalancer that maintains target allocation close to current (e.g. 35% QQQ, 30% SPY, 20% VXUS, 10% DFSI, 5% cash) with drift thresholds.
3. Risk module that calculates safe order quantities and rejects anything violating 1% risk / daily loss limit.
4. Full decision logging: "At timestamp T, equity was X, drift on QQQ was Y → decided to buy Z shares because... Expected impact: ..."
5. Kill switch via config file or simple command.
6. Telegram alerts for key events (large drift, order placed in paper, daily summary, errors).

## Risks & Open Questions for Reviewer
- How should we handle partial fills and order status tracking?
- Best way to persist "daily realized P&L" for the loss-limit check across restarts?
- Should the initial strategy be purely rules-based or allow light ML signals later (with heavy validation)?
- Any IBKR-specific gotchas with the current small account size (e.g. odd lots, minimums)?

## Request
Please review this high-level design + the PROTOCOL.md. Point out any gaps in safety, auditability, or implementation risks. Suggest concrete improvements before we move to detailed module design and first code.

This is still early — nothing will touch even paper orders until this design passes review and you + human approve the next handoff.
