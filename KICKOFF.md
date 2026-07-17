# KICKOFF.md — ibkr-auto-trader

**Collab bootstrapped**: 2026-07-03 by Grok acting as initial builder.

**Objective**: Design, implement, and safely validate an automated trading system for the user's taxable IBKR account (~$2k equity, aggressive equity tilt) using the collab-kit builder + independent reviewer + adversarial verification model.

**Current State**:
- PROTOCOL.md written with strict paper-first, risk management, no-lookahead, and auditability rules; REVIEWER-BRIEFING.md created.
- collab-kit slices 001–006 (collab_common → autopilot driver) and 012–017 (autonomous-mode architecture + evidence-gated done) are in `handoffs/done/`. The original `001-initial-design` proposal now lives in `handoffs/superseded/`.
- Trading system: PT-1/2/3/4a–c/6 + the reduce-only primitive have landed (`handoffs/done/` 027–034; PT-6 `42f0661`). Handoff `035` (PT-6 `RiskPlanner`/`PortfolioProjector`) is in flight; PT-5 (approver + loss-control enforcement) is drafted as `handoffs/draft/036`. Not yet built: PT-5, PT-8 (execution control), PT-12 (durable audit). Nothing trades live.

**Next Expected Actions** (evidence-gated autonomous flow):
1. Builder finishes handoff `035` (PT-6); then PT-5 (approver + loss-control enforcement, drafted as `036`) is the next milestone.
2. Reviewer (gpt-5.6-terra, repo-aware) plus breaker/verifier adversarial lanes verify the change.
3. `done_contract` gate evaluates all conditions (incl. source==tested + full pytest); only on pass does 027 advance to `handoffs/done/` — otherwise it stays `claimed/` with a `signoff_blocked` reason.
4. Continue draining subsequent PT slices the same way.

**How to Use This Collab**:
- Builder creates new handoffs in `pending/`.
- Reviewer claims from `pending/` → moves to `claimed/`, works, then to `done/`.
- All state is file-based and inspectable.
- Use `handoff` CLI (when installed) or manual file moves for now.

This collab is isolated and ready for rigorous, high-trust development of trading automation.
