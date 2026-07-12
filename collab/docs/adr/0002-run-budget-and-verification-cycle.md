# 2. Run budget and the verification cycle; "round" is retired

Status: accepted (2026-07-12; incorporates the contract review of the same date —
atomic per-handoff charging, immutable candidate/finding history, fail-closed lane
overflow, preserved metric semantics). Amends
[ADR-0001](0001-one-handoff-per-exchange.md).

## Context

The driver's autonomy is bounded by a single knob, `max_rounds`, but the word
*round* silently means four different things, and the accounting is inferred from
routing rather than stated:

- **Budget is charged to the first-routed seat, not the actual worker.** The seat
  a handoff is addressed `to` acts first and is treated as "the builder"; only its
  attempts draw down the cap (`autopilot.py:862`, `:877–881`, `:901–902`). ADR-0001
  cemented this as "builder = `to`, reviewer = `from`." That inference breaks for a
  **document-authoring** handoff routed `to: reviewer`: the reviewer becomes the
  charged primary worker. `test_autopilot.py:691` encodes exactly this — it expects
  `max_rounds=4` to produce **eight** actor turns, i.e. it counts turn-doubling, not
  work attempts.
- **Reviewers are "free" only by accident.** A reviewer draws down nothing *when it
  is the counterpart* — but not when it is `first_seat`. "Evaluation is free" is a
  real intent implemented by a fragile proxy (seat identity == routing direction).
- **Lanes are unbounded in cost.** Concurrent lanes are capped at six
  (`lanes.py:44`), but a lane makes one breaker call **plus one verifier call per
  finding** in a sequential loop with **no cap on findings or verifier calls**
  (`lanes.py:163`). The archived roll-up counts only seat turns:
  `calls == rounds_total == count of round-DONE events` (`run_history.py:138`) — lane
  invocations (`autopilot.lane` events) are not counted at all. So a "six-round" run
  can spend far more than six model calls, invisibly.
- **The first pass is reversed.** The reviewer is invoked to sign off, and the
  lanes run *inside* that sign-off evaluation; a confirmed defect then returns
  `blocked` (`autopilot.py:916`). So the first candidate can burn a reviewer call
  before verification has had a chance to reject it.
- **029 "feels confusing" for this reason.** Its send-back log says "round 8" /
  "round 10" — the raw actor sequence (`total_rounds`) — while the cap is checked
  against a *different* counter (`thread_rounds`). Same word, two meanings, one
  screen.

This is a terminology-and-accounting problem, not a missing feature. The fix is to
give each concept its own name, its own counter, and its own budget, owned by one
deep module, and to make verification precede every final reviewer decision.

## Decision

### D1 — Retire "round." Five named concepts, five accountings.

| Thing | Counts as | Budget |
|---|---|---|
| A source-changing action by the **work_owner** | work attempt + actor turn + model call | `max_work_attempts` |
| An **approver** decision (evaluation only) | actor turn + model call | `max_review_decisions_per_candidate` (= 1) |
| All selected lanes over **one candidate** (tests + breakers/verifiers) | verification pass | `max_verification_passes` |
| Each breaker or per-finding verifier invocation | verification/model call | `max_total_model_calls` + `max_findings_per_lane` |
| A driver handoff / stage transition | **nothing** | (state transition) |

No counter is ever named "round" again. `max_rounds` is renamed
`max_work_attempts`; telemetry exposes `actor_turns`, `verification_passes`,
`verification_calls`, and `total_model_calls` as **distinct** fields.

**Two of these are global ceilings, not per-kind budgets.** `max_total_model_calls`
is drawn down by **every** model call — work_owner, approver, breaker, and each
verifier — so `total_model_calls ≥ verification_calls` always; the per-kind budget
in each row applies *in addition*. `max_wall_clock` likewise bounds the whole
handoff's elapsed time regardless of kind. The other budgets
(`max_work_attempts`, `max_review_decisions_per_candidate`, `max_verification_passes`,
`max_findings_per_lane`) are per-kind.

### D2 — Explicit `work_owner` / `approver` (amends ADR-0001).

Roles are declared in handoff frontmatter, never inferred from routing:

```yaml
work_owner: builder     # the seat that may change source / author the artifact
approver: reviewer      # the seat that only evaluates a candidate
```

A reviewer seat that is *authoring* a document is a legitimate `work_owner`; a
reviewer that only assesses a candidate is the `approver` and never draws down
`max_work_attempts`. **Legacy compatibility:** when both fields are absent, default
`work_owner ← to` and `approver ← from` (ADR-0001's mapping), so existing handoffs
keep running while they are migrated.

**Validation (enforced at dispatch, fail-closed):**

- Both role fields must be present, or **neither** (legacy mode). A handoff with
  exactly one of `work_owner`/`approver` is rejected — a half-declared role is the
  ambiguity this decision exists to kill.
- Both roles must resolve to **configured seats**; an unknown seat is a fatal
  handoff error, not a silent fallback.
- `work_owner != approver`. A seat may not both author and approve the same
  candidate (separation of authority, ADR-0001 §18).
- `approver`, `breaker`, and `verifier` remain **three distinct evidence fields**
  in the ledger and are never collapsed. The 029 ledger is the standing proof of
  why: conflating the reviewer's sign-off with the verifier's verdict is exactly
  how a wrongly-refuted finding would masquerade as an approval.

### D3 — Candidate identity is a digest of source **and** verification plan.

A **candidate** is one immutable snapshot of the work_owner's output. Its id is a
SHA-256 digest over **both**:

1. the **source** (the `source_manifest` already computed for the ledger,
   `lanes.py:211`, promoted to first-class identity — it is already SHA-256), and
2. the **verification plan**: source roots, the test command, and the
   guardrail/lane configuration.

Binding the plan into the id is load-bearing: **a changed verification policy must
mint a new candidate**, so tightening guardrails or the test command can never
reuse evidence gathered under the looser old plan. Every lane result and every
approver decision **attests a specific candidate id**.

**History is immutable and per-candidate.** The verification ledger is no longer a
single per-handoff file overwritten each attempt (`lanes.py:86`) — that erases the
very candidate that produced a confirmed defect the moment a clean retry lands.
Instead:

- each candidate gets its **own immutable verification ledger**, keyed by candidate
  id and never overwritten;
- the handoff carries a **finding record** aggregating every confirmed finding
  across its candidates, each with `open | fixed` state and a
  `regression_evidence` pointer.

`done` must **resolve every prior confirmed finding** (fixed + regression-attested),
not merely observe that the *latest* candidate's ledger is clean. A defect found on
candidate N cannot be laundered away by a candidate N+1 that simply stopped
triggering the breaker.

### D4 — The candidate cycle (verification before the reviewer, always).

The order is fixed: **work_owner attempt → compute candidate id → verification pass
→ (at most) one approver decision.** The candidate id is computed *before*
verification, so the pass and the decision provably attest the same candidate.

```text
work_owner attempt N
  → compute candidate id  (source + verification plan; D3)
  → if id == prior candidate id  (no progress: no source/plan change):
        return to the work_owner, or pause on max_work_attempts.
        NEVER a second approver decision on an already-decided candidate.
  → ONE verification pass over candidate id  (tests + all selected lanes)
  → if verification-incomplete (lane overflow, D7): pause — not clean, not signed
  → if blocked (a lane confirmed a defect):  work_owner attempt N+1   # NO approver call
  → if clean:  ONE approver decision on candidate id
        → sign off      → done   (must also resolve all prior confirmed findings, D3)
        → changes req.  → work_owner attempt N+1
```

Verification runs **before** every final approver decision, including the first
candidate. The approver is never spent on a candidate that verification would
reject — the cost win — and (per D5) never twice on the same candidate id. A
**no-progress attempt** (the work_owner returned an identical candidate) never
manufactures a fresh approver decision; it bounces back to the work_owner and, if
the attempt budget is spent, pauses. The current "confirmed defects skip the
reviewer and enter builder↔lanes repair" special case (`autopilot.py:933–953`)
stops being a branch; it *is* the loop.

### D5 — One approver decision per unchanged candidate.

`max_review_decisions_per_candidate = 1`: the approver is invoked at most once per
candidate digest. Re-deciding requires a new candidate (a real work attempt). This
makes "reviewers are free of the work budget" true by construction, while still
counting each reviewer call against `actor_turns` and `max_total_model_calls`. This
is a **contract invariant, not a runtime calibration knob** — unlike the numeric
budgets it is not `control.json`-adjustable, and raising it above 1 would require a
future ADR, not a config change.

### D6 — One deep `RunBudget` module; charging is atomic and per-handoff.

A single module (`collab/tools/lib/run_budget.py`) owns every counter and every
limit; the driver goes through it instead of hand-rolling
`thread_rounds`/`total_rounds`/`fix_attempts` inline. Budgets:

- `max_work_attempts` (renamed from `max_rounds`)
- `max_review_decisions_per_candidate = 1`
- `max_verification_passes`
- `max_total_model_calls`
- `max_wall_clock`
- `max_findings_per_lane` (bounds verifier calls within a lane, D7)

**Atomic reservation (not check-then-charge).** Because lanes run in parallel
(`lanes.py:232`), a *may-I? … then charge* sequence races — two lanes both pass the
check, both dispatch, and the budget is silently overspent. Instead the budget is a
**persisted per-handoff record** (`autopilot/budget/<hid>.json`, atomic-written per
`[C16]`) carrying a monotonic **epoch** and the consumed counts. Every agent
dispatch — work_owner, approver, breaker, and each verifier call — first performs
an **atomic reserve** against that record; the reservation succeeds or the dispatch
does not happen. **A failed or timed-out call still consumes its reservation** (cost
was really incurred), so a flaky backend can't be used to farm free retries.

**Reopen = a new, human-authorized epoch — never a silent reset.** Reopening a
`done`/`capped` handoff does not resume the old counters and does not zero them in
place. It opens a **new budget epoch**, recorded with the authorizing human and a
link to the prior epoch's artifacts (ledgers, escalation). Counters start fresh for
the new epoch; the prior epoch's consumption stays immutable in the record. So "why
did this handoff get more attempts?" always has an audited answer.

Limits are live-adjustable via `control.json` as `max_rounds` is today
(`autopilot.py:872`); a mid-run raise applies to the **current** epoch and is
itself an audited event. Default numeric values are calibration knobs (see Open
questions), not part of this contract.

### D7 — Bound lane cost, and fail closed on overflow.

**Strategy (chosen): hard cap, not silent batch.** A lane verifies at most
`max_findings_per_lane` findings; verifying every finding in unbounded batches would
defeat the `max_total_model_calls` budget this ADR exists to enforce. Every breaker
and verifier invocation increments `verification_calls` and `total_model_calls`.

**Overflow is verification-incomplete — never clean.** If a breaker surfaces more
findings than the cap can verify, the un-verified excess is recorded explicitly
(count + the finding text, never silently dropped) and the pass's verdict is
**`verification_incomplete`**, a distinct state from `clean` and `blocked`.
`verification_incomplete` **prohibits autonomous sign-off**: the driver pauses
(D9) until a human either raises `max_findings_per_lane` (a new epoch, D6) and
re-verifies, or explicitly accepts the residual. A candidate whose book of findings
was only partially checked can never be treated as verified.

### D8 — Telemetry tells the truth, without corrupting history.

`run_history.build_summary` (`run_history.py:126`) gains distinct roll-up fields:
`actor_turns`, `verification_passes`, `verification_calls`, `total_model_calls`
(alongside per-seat partitions), under a bumped **accounting `schema_version`**.

**`calls`/`rounds_total` keep their original meaning.** Historically they mean
*actor-seat calls* (`run_history.py:138`) — never lane calls. Remapping them onto
`total_model_calls` would silently corrupt every archived run comparison. So they
remain **deprecated aliases of `actor_turns`** (their true historical meaning), and
the new schema version signals a reader to prefer the explicit fields. **Archived
`run.json` files are immutable** — never rewritten to the new fields; a reader keys
off `schema_version`.

The dashboard hero shows work attempts *and* total model calls, so a "3-attempt"
run can never hide a 40-call cost.

### D9 — Any exhausted budget pauses; `escalation.py` is the durable pause record.

Hitting **any** budget (`max_work_attempts`, `max_verification_passes`,
`max_total_model_calls`, `max_wall_clock`) — **or** a `verification_incomplete`
pass (D7) — is terminal: the driver pauses on that handoff, writes status `capped`,
and **never silently continues** past it.

`escalation.py` is **kept and redefined** — no longer a premature "one fix then
give up" policy (the driver stopped calling it; it survives only in
`dashboard_core.py`/`narrative.py` renders + tests). It becomes the durable,
human-facing explanation written **only on a terminal pause** (never on an ordinary
send-back). The artifact records:

- **why** it paused: the exhausted budget (or `verification_incomplete`), with
  **consumed / limit** values for **each** budget and the **budget epoch** (D6);
- the **candidate id** in force at the pause (source + plan digest, D3);
- the **confirmed blockers** and a link to that candidate's immutable **ledger**;
- the **finding record** (open / fixed across candidates) and the **send-back
  history** (from `sendbacks/<hid>.log`);
- the **human's next action** to clear it.

**Preserved across reopen.** When a paused handoff is reopened into a new epoch
(D6), its pause artifact is **not deleted** — it is marked **superseded** and linked
to the new epoch. The durable human-facing record must not vanish at the exact
moment it becomes evidence for "why did we grant more budget?"; each epoch's pause
artifact is retained and chained.

## Consequences

- **`test_autopilot.py:691` is rewritten.** It stops asserting turn-doubling
  (`max_rounds=4 → 8 turns`) and instead asserts the four budgets independently:
  work attempts, review decisions per candidate, verification passes, and total
  model calls.
- ADR-0001's "builder = `to`, reviewer = `from`" is superseded by explicit
  `work_owner`/`approver` (with the legacy default in D2). ADR-0001's one-handoff
  invariant and turn-artifact model are unchanged.
- `run.json` gains the D8 fields under a bumped accounting `schema_version`;
  `dashboard_web`/`narrative`/`dashboard_core` read the new counters; `calls`/
  `rounds_total` stay aliases of `actor_turns` and archived files are never
  rewritten.
- **The verification ledger becomes per-candidate and immutable** (`lanes.py:86`
  `ledger_path` is re-keyed by candidate id, no longer overwritten per handoff), and
  a handoff-level **finding record** is added; `done_contract` is extended to require
  every prior confirmed finding resolved (D3), not just a clean latest ledger.
- New per-handoff **budget record** (`autopilot/budget/<hid>.json`) with epoch +
  atomic reservations; the driver's inline `thread_rounds`/`total_rounds`/
  `fix_attempts` bookkeeping (`autopilot.py:863–906`) is replaced by `RunBudget`
  calls.
- The driver loop is re-centred on the D4 candidate cycle; `fix_mode` and the
  reviewer-skipping branch dissolve into the normal shape. A new
  `verification_incomplete` verdict joins `clean`/`blocked`.
- One small, deep module (`run_budget.py`) is the single place the accounting can
  be reasoned about or changed — the point of the ADR: give the implementation one
  contract to conform to.

## Open questions (calibration, not contract)

- Default numeric values for each budget (work attempts, verification passes, total
  model calls, wall-clock, findings/lane) — tuned against real runs, not fixed here.
  (The *strategy* on overflow is settled in D7: hard cap → `verification_incomplete`;
  only the number is open.)
- Migration window: how long the legacy `to`/`from` default in D2 stays before
  `work_owner`/`approver` become required.

(`max_review_decisions_per_candidate` is intentionally **not** listed here — D5
fixes it at 1 as a contract invariant; changing it is a future ADR, not calibration.)
