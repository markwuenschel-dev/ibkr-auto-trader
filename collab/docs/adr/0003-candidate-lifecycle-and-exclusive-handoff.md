# 3. The candidate lifecycle; verification and review run concurrently under an owned lease

Status: accepted (2026-07-13). Amends [ADR-0002](0002-run-budget-and-verification-cycle.md)
and [ADR-0001](0001-one-handoff-per-exchange.md). **Supersedes ADR-0002 D4's ordering
only** (verification strictly *before* the reviewer); every budget and immutable-evidence
principle in ADR-0002 is preserved unchanged.

## Context

ADR-0002 retired "round," named the five budgets, and defined the candidate cycle:
*work_owner attempt → candidate id → one verification pass → at most one approver
decision.* Implementing it surfaced three things ADR-0002 either ordered sub-optimally or
left as an emergent invariant rather than an enforced one.

- **The ordering is stricter than it needs to be.** ADR-0002 D4 runs verification
  *entirely before* the reviewer so the approver is never spent on a candidate verification
  would reject. That goal is real, but the sequential form serialises two independent,
  slow, read-only evaluations. A candidate's lanes (breaker→verifier) and its reviewer
  read the *same immutable candidate*; neither writes. There is no data dependency forcing
  one to finish before the other starts — only the *decision* must wait for both.
- **Exclusivity is emergent, not owned.** ADR-0001 keeps `claimed ≤ 1` by loop shape:
  the driver selects one root, alternates seats in memory, and stops on any non-closed
  outcome. But `hc.claim` is a **per-handoff** `os.link` compare-and-swap
  (`handoff_core.py:300`), not a board-wide lock. Two driver processes can each claim a
  *different* pending handoff and run in parallel — the exact uncoordinated-control failure
  the one-handoff invariant exists to prevent. The invariant is asserted by convention, not
  guaranteed by a lock.
- **Non-mutation is self-reported.** The candidate model needs the reviewer and the
  breaker/verifier lanes to *evaluate* a candidate without changing it — otherwise "the
  approver and the verifier attest the same candidate id" (ADR-0002 D3) is a fiction. But a
  reviewer's honesty about not editing is just a claim, and the real seat capabilities today
  contradict the intent: the OpenAI repo adapter's `--write` grants file-write **and**
  command-run together (`openai-repo-seat.py:400`, `:218-229`), and the Claude
  breaker/verifier seats run under `acceptEdits` (`seats.json:185+`). An "assessment" actor
  can silently mutate the source it is judging.

The live 030 failure is the same family of problem at the seat layer: `load_seats:244` blindly
concatenates a seat's model-family-specific `model_args` onto whatever adapter its `model`
resolves to, so Claude-only `--permission-mode`/`--allowedTools` reach the OpenAI adapter,
which exits 2, and every lane fails. Capability is composed by string concatenation, never
checked.

## Decision

### D1 — Verification and review run concurrently; the aggregate decides (supersedes ADR-0002 D4 ordering).

Within one candidate's assessment, the reviewer and all selected lanes are dispatched
**concurrently** over the same immutable candidate id. The reviewer's approval is
**provisional** — it is not a state transition, only one input. The candidate's outcome is
the **aggregate** of both, resolved by an explicit merge:

| Signal | Effect |
|---|---|
| Reviewer **evidence-backed** blocking finding (correctness / contract / safety / regression, non-empty evidence) | **blocks** → `repair_required` |
| Lane **confirmed** blocker (a breaker finding a verifier confirmed) | **blocks** → `repair_required` |
| Lane **refutes** a finding | clears only *that lane's* finding — it never erases a reviewer blocker |
| Reviewer concern **without** supporting evidence | **advisory** — visible, never blocks |
| Reviewer output malformed / unparseable | `verification_incomplete` (pause; never a silent pass) |
| Lane / tool / seat-argv failure | `infrastructure_blocked` (pause; command + error attached) |
| Findings exceed `max_findings_per_lane` (unverified excess, ADR-0002 D7) | `verification_incomplete` (pause) |
| No open blocker after merge **and** every prior confirmed finding resolved (ADR-0002 D3) | `approved` |

ADR-0002's cost guarantee is **preserved**: the approver is still charged at most once per
candidate (`max_review_decisions_per_candidate = 1`, ADR-0002 D5), and a no-progress attempt
(identical candidate id after a repair packet) still never manufactures a fresh decision — it
pauses (`no_progress`). What changes is only that the one reviewer decision is *gathered in
parallel with* verification instead of strictly after it. The four outcomes —
`approved | repair_required | infrastructure_blocked | verification_incomplete` — replace the
ambiguous `blocked`/`stalled` verdicts.

### D2 — Exclusivity is owned by a global lease, not emergent.

A single **`ActiveHandoffLease`** (`autopilot/active.lease`, managed by `handoff_core.py`
under the existing `cc.collab_lock` cross-process lock) grants exactly one run the right to
hold the board. A run **acquires the lease before it claims any handoff**, renews it via the
existing `_Heartbeat` thread, and releases it only on `done`, a terminal pause, or `reopen`.
`acquire` fails if a *live* lease held by a different `run_uid` exists; a **stale** lease
(dead pid / heartbeat past its TTL) is reclaimable, and every reclaim is an audited event.

The board-state machine (`pending/claimed/done/archive`) and its `os.link` CAS are unchanged
— the lease sits *above* it. Builder, reviewer, breaker, and verifier jobs all run beneath the
one lease and may never `claim`, `reopen`, or `advance` another handoff. The next slice cannot
start until the current one reaches `done/` **and** the lease is free. ADR-0001's `claimed ≤ 1`
stops being a convention and becomes a guarantee.

### D3 — Assessment actors are non-mutating by enforced policy, not by self-report.

Every seat carries a typed **`SeatPolicy.access`**, and the adapter must be able to *enforce*
it or the seat does not compile:

| Role | Policy | Capability |
|---|---|---|
| builder / work_owner | `write` | file writes + allow-listed command run |
| reviewer / approver | `read` | read-only tools; no file write, no command run |
| breaker / verifier | `read_test` | read + run **fixed allow-listed test commands** (`pytest / ruff / python / uv`); **no** source writes |

`compile_seat()` resolves a seat's `model` to a typed adapter and calls
`adapter.supports(policy)`; an adapter that cannot express the policy is a **fatal compile
error**, raised *before* any argv is produced or persisted — checked both when a seat is saved
and again when a run claims work. This is where the 030 argv bug dies: an adapter renders only
flags it understands, so a Claude-only permission flag can never reach the OpenAI adapter.

Enforcement has real teeth:

- The current OpenAI repo adapter **cannot** express `read_test` (its `--write` couples
  write + run). It gains a decoupled `--run-checks` mode (run the allow-list, no writes); until
  a seat's adapter can enforce `read_test`, an OpenAI breaker/verifier seat is **rejected**
  rather than silently upgraded to `--write`.
- The Claude adapter's `read_test` emits a **non-editing** permission (no `acceptEdits`; only
  the test/lint `Bash(...)` tools allow-listed) — correcting today's `acceptEdits` seats.
- Belt-and-suspenders: an assessment snapshots the candidate's `source_manifest` before the
  fan-out and asserts it is **byte-identical afterward**. Any drift is `infrastructure_blocked`
  — an assessment actor that mutated the source it was judging can never yield an `approved`.

### D4 — One deep module owns the assessment policy.

`candidate_assessment.py` owns candidate identity, the finding-history merge, outcome
classification, and retry eligibility behind three operations — `prepare` (compute id, cache
lookup, report reusable evidence), `complete` (merge findings, classify via D1's table,
persist the immutable aggregate), and `retry` (finish an `infrastructure_blocked` /
`verification_incomplete` assessment by reusing prior successful evidence and running only the
missing work — **never** the builder). The orchestrator dispatches model calls and drives
transitions; it does not re-implement the merge. Candidate identity **extends** ADR-0002's
`run_budget.candidate_id` by folding in the contract revision, the assessment-plan revision,
the reviewer rubric, and a seat-profile fingerprint, so a changed *rubric or seat* also mints a
new candidate — evidence can never be reused under a materially different assessment.

### D5 — Operator actions are durable requests the lease-holder consumes; the web layer never executes work.

A dashboard action that needs orchestration work done (Retry-verification, the 030
adopt-source recovery) **writes a durable request** (`autopilot/requests/<hid>.json`); the one
lease-holding driver consumes it. The web handler never spawns an assessment itself — doing so
would stand up a second, unleased locus of control, re-creating exactly the parallel-control
problem D2 removes. Human overrides keep their true meaning: **approval** is
`advance_handoff → hc.done`; **reopen** is a *retry* that opens a new budget epoch (ADR-0002
D6), never an automated reviewer approval. Both are audited.

### D6 — Terminal states tell the truth; recovery paths are explicit.

The driver records `closed | capped | infrastructure_blocked | verification_incomplete |
stopped | paused` distinctly; a user **stop is never archived as `done`**. Because 030's failed
run produced no candidate-assessment artifact to reuse, "retry verification" is not applicable
to it — instead an explicit, human-triggered **adopt-current-source-as-candidate** recovery
(preflight the now-compiled seats, snapshot the working tree as the candidate, assess it with
no builder call) exists alongside `retry` (reuse partial evidence) and `reopen` (new epoch for
fresh builder work).

## Consequences

- ADR-0002 D4's *ordering* is superseded: verification no longer strictly precedes the
  reviewer. Everything else in ADR-0002 stands — the five budgets, atomic per-handoff
  charging, `max_review_decisions_per_candidate = 1`, per-candidate immutable ledgers, the
  cross-candidate finding record, and "`done` resolves every prior confirmed finding."
- `handoff_core.py` gains `ActiveHandoffLease`; the four board states and their CAS are
  untouched. The driver acquires the lease before `hc.claim` and holds it for the whole slice.
- Seats gain typed adapter profiles + an `access` policy; `compile_seat` replaces the
  `load_seats:244` concatenation and is the single seat-capability gate. `openai-repo-seat.py`
  gains `--run-checks`. Legacy explicit-`cmd` seats remain runnable but non-switchable until
  migrated.
- `candidate_assessment.py` is the deep module the assessment policy lives in; the driver
  loop's `_run_turn` / `_verify_fix_lanes` / `thread_rounds` / `fix_mode` machinery dissolves
  into `prepare → assess(reviewer ∥ lanes) → complete`.
- `escalation.py` (dead relative to the driver since 2026-07-11) is revived as the durable
  pause record, per ADR-0002 D9, now written on `no_progress` / `infrastructure_blocked` /
  `verification_incomplete` as well.
- The dashboard surfaces the active handoff, candidate id, assessment stage, blocker/advisory
  counts, budget consumption, and the exact pause reason; Retry-verification and adopt-source
  are operator requests, not web-executed work.

## Open questions (calibration, not contract)

- Lease heartbeat TTL and the stale-reclaim grace window — tuned so a briefly-paused driver is
  not reclaimed out from under itself, but a dead one frees the board promptly.
- Whether `read` (reviewer) should permit a strictly read-only `run_command` (e.g. `--collect-only`)
  or remain no-run — left to the seat catalog, not fixed here.
