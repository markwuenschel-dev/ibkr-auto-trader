# Dashboard human-observability remediation ledger

Date: 2026-07-22
Mode: candidate-by-candidate integrity loop
Evidence: `docs/evidence/dashboard-human-observability-2026-07-22.md`

Priority uses the repository audit formula:

`severity + confidence + leverage + locality + testability - blast radius - regression risk - human-decision risk`

## Ordered candidates

| ID | Candidate | S | C | L | Lo | T | B | R | H | Priority | State |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| OBS-001 | Canonical model-attempt identity and gateway/Langfuse reconciliation | 5 | 5 | 5 | 4 | 5 | 3 | 2 | 1 | 18 | implemented; live proof partial |
| OBS-002 | Honest run plan, work-unit accounting, and terminal decision record | 5 | 5 | 5 | 3 | 5 | 4 | 3 | 1 | 15 | implemented; contract proven |
| OBS-003 | Durable complete run evidence and replay contract | 5 | 5 | 5 | 3 | 5 | 4 | 3 | 1 | 15 | implemented; browser replay proven |
| OBS-004 | Per-model live presence and coordinated operator views | 4 | 5 | 5 | 3 | 4 | 4 | 3 | 1 | 13 | implemented; browser proven |
| OBS-005 | Candidate lineage, requirements, dispositions, and quality evidence | 5 | 4 | 5 | 2 | 4 | 5 | 4 | 2 | 9 | implemented; real run proof pending |
| OBS-006 | Scale, accessibility, interruption, and operator usability proof | 4 | 4 | 4 | 2 | 4 | 4 | 3 | 2 | 9 | partial; external gates open |

## OBS-001 — canonical model-attempt identity and reconciliation

**Current friction.** The orchestration run UID is minted after the repository slug is assigned to `rid`, but seat dispatch exports `COLLAB_RUN_UID=rid`. The app ledger, LiteLLM session, and Langfuse session therefore carry `ibkr-auto-trader`, not `20260721T234615Z-140256`. Lane dispatch does not pass any correlation environment. Attempt records are appended only after the HTTP call finishes, and `langfuse_export` remains `unverified` without a separate manual append.

**Root seam.** Autopilot run identity → seat/lane environment → adapter request metadata → LiteLLM tags/session → Langfuse observation → retained dashboard join.

**Integrity rule.** Every model attempt receives one immutable app request ID and the actual run UID before dispatch; every transport and telemetry record can be joined without prompt inspection; planned-but-not-started is distinct from started-but-not-exported.

**Evidence.** `collab/tools/lib/autopilot.py:1347-1362`, `:882`; `collab/tools/lib/lanes.py:173-182`; `collab/tools/lib/llm_gateway.py:194-276`; fixed-window LiteLLM/Langfuse evidence in the companion report.

**Enforceable check.** A fake gateway plus fake Langfuse query must prove queued → started → gateway accepted → completed → telemetry verified for the same request/run/seat/candidate identifiers, including retry and failure variants. An integrated live proof must reconcile app attempts = matching LiteLLM spend rows = matching Langfuse generations for a unique run UID.

**Boundary.** This candidate does not redesign round policy, dashboard layout, or candidate scoring.

## OBS-002 — honest run plan, work-unit accounting, and terminal decisions

**Current friction.** `calls`, `rounds_total`, actor turns, verification calls, and provider HTTP attempts name different units. The UI labels the deprecated `max_rounds` alias as a round budget. A run stopped by `infrastructure_blocked` archives as phase `capped`, with no durable stop decision or consequence.

**Root seam.** `RunBudget` counters → orchestration decisions → event schema → archive summary → operator language.

**Integrity rule.** A run has an immutable plan snapshot, typed work units, explicit branch/stop decisions, and one authoritative terminal outcome. Counters never reuse one label for different units.

**Enforceable check.** Scenario tests cover success, rejection/revision, infrastructure failure, human pause, cancellation, retry, and each budget exhaustion independently. Each scenario asserts planned work, completed work, skipped work with reason, remaining work, and terminal decision.

**Boundary.** This candidate does not determine subjective quality scores; it makes orchestration facts honest.

## OBS-003 — durable complete run evidence and replay

**Current friction.** `run_history.py` archives three files and `dashboard_core.run_detail()` returns only `summary`, `events`, and `lanes`. Budget, attempts, telemetry verification, assessments, requirement evidence, and dispositions remain outside the replay boundary. Event display tails at 200 and history pruning keeps a small fixed run count.

**Root seam.** Append-only live evidence → atomic final manifest → archive index → historical API → replay renderer.

**Integrity rule.** Completion atomically seals a content-addressed manifest of every required artifact; replay reads only the archive and yields the same operator facts as the final live snapshot; retention and pruning are explicit policy with evidence of what was removed.

**Enforceable check.** Crash/torn-write, interrupted run, high-volume event, archive/reload, and prune-policy tests; manifest hashes must fail closed on missing or altered artifacts.

**Boundary.** Raw prompts and responses remain excluded unless an explicit privacy policy permits them.

## OBS-004 — per-model live presence and coordinated views

**Current friction.** One scalar `active_seat` cannot represent concurrent model attempts. Lane execution is compressed to an aggregate seat, and narrative strings such as “thinking,” “finished turn,” and “lane done” omit what is being tested, evidence links, consequences, and next actions.

**Root seam.** Attempt/read models → SSE event projections → roster, timeline, candidate workspace, evidence inspector, telemetry explorer, and final decision views.

**Integrity rule.** Every configured model always has visible roster status; concurrent attempts remain independently inspectable; every transition states actor, action, object, reason, evidence, consequence, and next action.

**Enforceable check.** DOM/HTTP projection tests for concurrent cards, reorderable retries, planned/skipped states, deep links, filters, and replay parity; browser performance budgets at small and large run sizes.

**Boundary.** The UI consumes canonical facts and never invents provider reachability or acceptance from elapsed time alone.

## OBS-005 — candidate lineage, requirements, dispositions, and quality

**Current friction.** Some candidate assessment artifacts exist, but archived run detail does not expose proposal lineage, immutable requirement evidence, critic identity, full rejection reasons, disagreement, uncertainty, test quality, downstream effects, or final candidate disposition.

**Root seam.** Handoff requirements → candidate proposal/version lineage → reviewer/breaker/verifier evidence → decision record → final acceptance or rejection.

**Integrity rule.** Every candidate version is immutable and linked to its parent, requirements, findings, quality dimensions, test evidence, dissent, uncertainty, and final disposition. “Accepted” requires an explicit oracle, not a generic reviewer completion.

**Enforceable check.** Table-driven assessment scenarios prove full acceptance, partial acceptance, rejection, supersession, unresolved dissent, weak-test rejection, and human override with preserved provenance.

**Boundary.** Automated quality measurements are evidence for a human-authorized policy; they do not silently expand autonomous authority.

## OBS-006 — performance, accessibility, interruption, and operator proof

**Current friction.** The current evidence proves a screenshot and backend tests, not operator comprehension, high-volume rendering, reconnect/cancellation behavior, keyboard navigation, or full multi-model historical replay.

**Root seam.** Bounded server queries → incremental client rendering → SSE reconnect/resume → accessible interactions → scripted operator tasks.

**Integrity rule.** The dashboard remains responsive and truthful under concurrency, retries, partial telemetry, interruption, and large histories; an operator can answer the goal’s key questions without reading raw JSON.

**Enforceable check.** Performance budgets, accessibility audit, failure injection, reconnect/resume, cancel/retry, and timed operator task protocol using a real gateway-backed run.

**Boundary.** Shipping readiness requires both machine verification and human usability evidence.

## Delivery gate

Production changes start with OBS-001 only. Each candidate must:

1. define its public read/write schema and compatibility policy;
2. add a failing contract test against that schema;
3. implement the smallest connected slice;
4. run targeted and authoritative verification;
5. append evidence and disposition here before the next candidate begins.

## Current evidence classification — 2026-07-24

### Confirmed bugs fixed in the candidate

- The orchestration parent execution and the canonical gateway attempt are now separately
  counted and linked by the real parent execution UUID; provider-response totals no longer
  count orchestration wrappers as upstream responses.
- Haiku now uses the canonical LiteLLM alias instead of the direct-provider path.
- Streaming and non-streaming calls become visible before completion, and provider-specific
  stream completion is reconstructed without retaining response bodies.
- Model-attempt persistence failures and redacted call-ledger persistence failures have
  separate durable health records, retained in sealed history.
- Candidate producer lineage now points to the durable orchestration execution, not a
  synthetic actor-turn label.
- Production `orchestrator` failures before gateway invocation now match the roster
  classifier. Final stop decisions reconcile every uninvoked eligible model to a reasoned
  skip, while missing gateway, evaluation, or telemetry evidence remains explicitly
  incomplete.
- Run-archive persistence now has a separate durable health record and dashboard dimension;
  it is no longer conflated with per-handoff lifecycle-history persistence.
- Candidates, validations, requirements, dispositions, and round decisions now use one
  health-aware shared run-evidence append contract. A failure is run-scoped, redacted,
  archived, and visible rather than being only an exception or stderr line.
- The repository-aware OpenAI-compatible adapter now emits redacted, run-correlated tool
  lifecycle records around actual tool dispatch. Attempt reduction preserves those records,
  and both live and sealed dashboard views show a compact tool-activity summary.

### Proven environment or external blockers

- The fresh four-alias proof reached LiteLLM and Langfuse for all four aliases. GPT, Gemini,
  and Haiku returned successfully; xAI rejected Grok with HTTP 403 because provider credit or
  monthly capacity was exhausted. This is explicit missing runtime evidence, not success.
- The complete repository-standard verifier, Next production build, and latest Chromium
  observability/reconnect scenarios now pass in the landing environment.

### Plausible leads still open

- The `openai-repo-seat.py` tool-lifecycle contract is implemented and test-proven, but a real
  provider run that actually invokes a tool has not yet been reconciled through the dashboard
  and sealed replay. Other opaque CLI adapters remain outside that claim unless they export
  the same explicit lifecycle records.
- A representative real candidate/evaluation/disposition run under one UID has not yet been
  completed across all four upstream providers.

### Ruled-out leads

- Missing model presence is not caused by waiting for Langfuse: cards are created from the
  orchestration attempt before the provider or telemetry system responds.
- A configured maximum of three is not evidence that three rounds must run. The reproduced
  two-of-three case is a structured early-stop decision after an accepted candidate satisfies
  the completion rule.

### Unrelated technical debt

- The repository's existing broad Pyright run reports legacy optional-import and typing
  findings in `autopilot.py`; the changed observability modules and fixture scripts type-check
  independently with zero errors.
