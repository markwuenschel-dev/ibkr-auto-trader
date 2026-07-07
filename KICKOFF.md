# KICKOFF.md — ibkr-auto-trader

**Collab bootstrapped**: 2026-07-03 by Grok acting as initial builder.

**Objective**: Design, implement, and safely validate an automated trading system for the user's taxable IBKR account (~$2k equity, aggressive equity tilt) using the collab-kit builder + independent reviewer + adversarial verification model.

**Current State**:
- PROTOCOL.md written with strict paper-first, risk management, no-lookahead, and auditability rules.
- REVIEWER-BRIEFING.md created.
- First handoff (001-initial-design) is in `handoffs/pending/`.

**Next Expected Actions**:
1. Independent reviewer claims and reviews handoff 001.
2. Adversarial regression hunt triggered for risk/safety aspects.
3. Human (Mark) reviews via Telegram or direct file comments.
4. Once approved, move to detailed module design + first working paper-trading skeleton.

**How to Use This Collab**:
- Builder creates new handoffs in `pending/`.
- Reviewer claims from `pending/` → moves to `claimed/`, works, then to `done/`.
- All state is file-based and inspectable.
- Use `handoff` CLI (when installed) or manual file moves for now.

This collab is isolated and ready for rigorous, high-trust development of trading automation.
