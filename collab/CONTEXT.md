# Context — collab-kit

Glossary for the file-based builder + independent-reviewer orchestration layer.
Terms only — no implementation details. The driver is **candidate-based**
(ADR-0002/0003): it drives one handoff as a sequence of candidates, each
assessed and classified, bounded by named budgets — not a turn-doubling round loop.

## Glossary

### Handoff
A single unit of review work. Exactly one file on the board, flowing through the
four states below. A handoff reaches **done only when a candidate is approved and
its evidence contract is satisfied** — never as a side effect of the conversation
about it. "One handoff at a time" is now *enforced* by the board **lease** (below),
not merely emergent: at most one handoff sits in `claimed/` at any instant, and at
most one driver holds the board.

### Candidate
One immutable snapshot of the worker's output judged under a fixed plan. Its
**identity** is a hash of the source manifest **and** the verification plan (source
roots, test command, lane config) **and** the reviewer rubric + seat profile — so a
changed rubric, plan, or seat mints a *new* candidate and evidence can never be
reused under a different lens. A byte-identical re-submission is the *same* candidate:
its completed assessment is reused verbatim (zero new model calls), which is also how
a repair that changed nothing is detected as **no-progress**.

### Turn
One builder or reviewer message. A turn is **not** a handoff — it never appears on
the board. Turns are persisted as inert reply artifacts + `autopilot.round` events.
Agent output is untrusted **data**, never control-plane ([C38]).

### Assessment
The judgement of one candidate: the reviewer **decision** (a `[[SIGNOFF]]` from a
`can_sign_off` seat ⇒ accept; withheld ⇒ one blocking concern) run **in parallel**
with the adversarial **lanes** (structured evidence). The reviewer stays free-text;
the orchestrator synthesizes the whole-candidate report. Classified into one outcome:
- **approved** — no open blocking finding survives.
- **repair_required** — an open, evidence-backed blocker (reviewer-withheld or
  lane-confirmed); the worker gets the exact findings and the loop retries.
- **infrastructure_blocked** — a lane tool failed / the source drifted mid-assessment.
- **verification_incomplete** — an unparseable review or a findings overflow.
Only **approved** + a satisfied evidence contract reaches `done`.

### Budget
The five named, separately-accounted bounds (ADR-0002) — no overloaded "round":
work attempts, review decisions **per candidate** (a fixed invariant of 1),
verification passes, total model calls, and wall-clock. The worker's attempts draw
down the work-attempt budget; on exhaustion the driver escalates. `--max-rounds` is
a **deprecated alias** for the work-attempt budget. A dashboard **reopen** opens a
new, human-authorized budget **epoch** (counters reset; the closed epoch stays
immutable in the record, so "why did it get more budget?" always has an answer).

### Board lease
One run's exclusive grip on the board (`autopilot/active.lease`), acquired before it
claims any handoff and renewed by the driver heartbeat. A second run cannot start
while a **live** lease is held; a **stale** lease (crashed driver, heartbeat past the
TTL) is reclaimable, and every reclaim is audited.

### Sign-off
The reviewer's judgement that the work is accepted. Necessary but not sufficient:
it only advances a candidate to `done` when the §18.3 evidence contract is also
satisfied (independent approver, required lanes ran clean, `source == tested`,
tests passed, repo-aware preflight). No seat may approve its own work.

### Escalation
The durable pause record the driver writes when a handoff cannot be closed within
budget (`autopilot/escalations/<hid>.md`) — the reproduced defect(s) plus the
terminal reason (`budget_exhausted`, `no_progress`, `infrastructure_blocked`,
`verification_incomplete`, `contract_unsatisfied`). It survives the driver process
and is the "call to a human" end of the loop.

### Operator request
A durable human action filed from the dashboard (`autopilot/requests/<hid>.json`)
that the driver consumes on its next pass — honoured even if no driver was running
when it was filed. **retry** re-drives a paused handoff (fresh worker attempt, new
budget epoch); **adopt** takes the current on-disk source as the candidate without a
worker turn. Either way the evidence contract still gates the close — a request can
never *force* a `done` ([C36]).

### Exchange
The full worker↔reviewer back-and-forth while a single handoff is `claimed`. Ends
when a candidate is approved + contract-satisfied (→ `done`) or a budget/no-progress
terminal is reached (→ escalated, awaiting a human).

### States
- **pending** — created, waiting for its recipient.
- **claimed** — its recipient is actively working the exchange (or paused/escalated,
  awaiting a human/operator request). At most one.
- **done** — approved and accepted.
- **archive** — filed away after done.
