# Context — collab-kit

Glossary for the file-based builder + independent-reviewer orchestration layer.
Terms only — no implementation details.

## Glossary

### Handoff
A single unit of review work. Exactly one file on the board, flowing through the
four states below. A handoff reaches **done only when the verifier signs off** —
never as a side effect of the conversation about it. "One handoff at a time"
means: at most one handoff sits in `claimed/` at any instant.

### Turn
One builder or reviewer message in the exchange *about* a handoff. A turn is
**not** a handoff — it never appears on the board and never occupies a state
directory. Turns are persisted as artifacts (the reply log) and events, so the
transcript survives, but they do not multiply the board.

### Exchange
The full builder↔reviewer back-and-forth that happens while a single handoff is
`claimed`. Composed of turns. Ends when the handoff is signed off (→ `done`) or
the round budget is exhausted (→ capped, awaiting a human).

### Sign-off
The verifier's judgement that the work is accepted. Necessary but not sufficient:
it only advances a handoff to `done` when the evidence contract is also satisfied
(independent approver, adversarial lanes ran clean, source == tested, …).

### States
- **pending** — created, waiting for its recipient.
- **claimed** — its recipient is actively working the exchange. At most one.
- **done** — signed off and accepted.
- **archive** — filed away after done.
