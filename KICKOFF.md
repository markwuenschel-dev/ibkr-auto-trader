# KICKOFF.md — ibkr-auto-trader

**Collab bootstrapped**: 2026-07-03 by Grok acting as initial builder.

**Objective**: Design, implement, and safely validate an automated trading system for the user's taxable IBKR account (~$2k equity, aggressive equity tilt) using the collab-kit builder + independent reviewer + adversarial verification model.

**Current State**:
- PROTOCOL.md written with strict paper-first, risk management, no-lookahead, and auditability rules; REVIEWER-BRIEFING.md created.
- collab-kit slices 001–006 (collab_common → autopilot driver) and 012–017 (autonomous-mode architecture + evidence-gated done) are in `handoffs/done/`. The original `001-initial-design` proposal now lives in `handoffs/superseded/`.
- Trading system: PT-1 (frozen domain models) is done. The live pending handoff is `handoffs/pending/027-pt-2-sqlite-state-store.md` — PT-2, a SQLite state store (positions cache, restart-surviving daily P&L for the −3% lockout, idempotency keys).

**Next Expected Actions** (evidence-gated autonomous flow):
1. Builder (Claude opus) claims handoff 027 and implements PT-2.
2. Reviewer (gpt-5.5, repo-aware) plus breaker/verifier adversarial lanes verify the change.
3. `done_contract` gate evaluates all conditions (incl. source==tested + full pytest); only on pass does 027 advance to `handoffs/done/` — otherwise it stays `claimed/` with a `signoff_blocked` reason.
4. Continue draining subsequent PT slices the same way.

**How to Use This Collab**:
- Builder creates new handoffs in `pending/`.
- Reviewer claims from `pending/` → moves to `claimed/`, works, then to `done/`.
- All state is file-based and inspectable.
- Use `handoff` CLI (when installed) or manual file moves for now.

This collab is isolated and ready for rigorous, high-trust development of trading automation.
