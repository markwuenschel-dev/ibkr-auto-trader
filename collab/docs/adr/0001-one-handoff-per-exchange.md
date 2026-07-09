# 1. One handoff per exchange; turns are not handoffs

Status: accepted (2026-07-08)

## Context

The autopilot driver ran a builder↔reviewer exchange by minting a **new handoff
file for every conversation turn** (builder reply, reviewer reply, …) and calling
`hc.claim` on each. Because the state machine only moves a handoff out of
`claimed/` via `done` (`claimed → done`), and `done` fires only on sign-off, every
intermediate turn stranded in `claimed/`. A single exchange left three or four
files in `claimed/` at once, violating the "one handoff at a time" invariant the
project owner has stated repeatedly.

The four-state machine (`pending → claimed → done → archive`) offers **no legal
resting place** for an intermediate turn other than `claimed` — so keeping turns
as handoffs and keeping `claimed ≤ 1` are mutually exclusive.

## Decision

A **handoff is the unit of review work**, not a message. The builder↔reviewer
**turns are not handoffs** — the driver alternates the two seats in-memory while a
single handoff stays `claimed`, and persists each turn only as a reply artifact +
event. The handoff moves `claimed → done` **only** on an accepted sign-off
(token + satisfied evidence contract). See [CONTEXT.md](../../CONTEXT.md).

The two seats are identified from the root handoff's frontmatter: builder = `to`,
reviewer = `from`. The done-contract is evaluated with those explicit seat
identities rather than inferring "builder" from the inbound's `from`.

## Consequences

- `claimed/` holds at most one file for the whole exchange — the stated invariant,
  true by construction rather than by remembering to transition each turn.
- No permanent handoff id is burned per turn; the board is not polluted.
- The driver's core loop (`run` / `run_round`) is rewritten: turn-taking moves
  from "follow the reply-handoff chain" to "alternate seats in memory."
- The two-live-session (watcher) mode still uses handoff files to pass control
  between separate agent sessions; this decision governs the single-driver
  autopilot only.
- Tests that asserted per-turn reply-handoff creation are rewritten to the
  turn-artifact model.
