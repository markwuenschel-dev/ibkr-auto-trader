# Dashboard human-observability implementation plan

> Delivery is an ordered integrity loop. Complete and verify one candidate before opening the next. Preserve the existing `/api/state`, `/api/stream`, `/api/runs`, and `/api/run?id=` routes; all response changes are additive until the browser and TUI have migrated.

**Goal:** Make every planned and attempted model, orchestration decision, candidate disposition, validation result, and telemetry outcome visible live and replayable after restart, with evidence-aware operator explanations and a real four-model proof.

**Architecture:** One append-only evidence graph is the source for live and historical projections. Orchestration emits facts before work begins. Transport enriches the same attempt identity. LiteLLM remains the sole generation-telemetry exporter; a read-only reconciler joins Langfuse observations back to app request IDs and persists verified or failed telemetry outcomes. Run completion seals a manifest over roster, plan, attempts, decisions, candidates, validations, dispositions, and provenance. The dashboard renders coordinated operator, model, validation, and diagnostic projections and never infers success from missing data.

**Compatibility:** Existing status/event/model-call JSON continues to load. New records use versioned additive schemas. Historical runs without evidence render `unknown`/`not recorded`, never synthetic success.

## Contract vocabulary

- **Run:** one orchestration invocation identified by the minted timestamp/PID `run_uid`.
- **Work attempt:** a candidate-producing builder action counted by `RunBudget.work_attempts`.
- **Actor turn:** one builder or reviewer subprocess invocation.
- **Model attempt:** one HTTP or direct-provider invocation, including retries and failures before response.
- **Verification call:** one breaker/verifier model invocation.
- **Round:** an operator grouping with an explicit boundary and decision record; never a synonym for calls.
- **Expected participant:** a configured role/model in the immutable execution roster.
- **Telemetry verified:** a read-only join proves a Langfuse observation for the app request ID.
- **Unknown:** evidence has not established the fact; absence is never promoted to healthy.

## Public additive seams

1. `autopilot/model-events.jsonl`: immutable `model_attempt_event` records with `schema_version=2.0`, stable `event_id`, `attempt_id`, `request_id`, `run_uid`, role/model/candidate identifiers, lifecycle state, timestamps, provenance, safe detail, and optional transport/telemetry facts.
2. `autopilot/run-events.jsonl`: immutable run plan, round decision, candidate, validation, disposition, human-action, persistence-failure, and terminal events sharing the provenance envelope.
3. `/api/state`: add `run_plan`, `execution_roster`, `model_activity`, `latest_decision`, `evidence_health`, and `operator_summary`.
4. `/api/run?id=<run_uid>`: add complete `manifest`, `roster`, `plan`, `attempts`, `decisions`, `candidates`, `validations`, `dispositions`, `requirements`, `telemetry`, `narrative`, and `evidence_health` sections.
5. `/api/stream`: retain cursor/id semantics; publish snapshots whose model cards are created from pre-transport attempt events.

The goal text already specifies the externally observable behavior for these existing routes. Tests will pin only additive fields and truthful state transitions; they will not expose chain-of-thought or prompt/output bodies.

---

## Candidate OBS-001 — canonical attempt identity, lifecycle, and telemetry reconciliation

### Task 1: Add a versioned immutable model-attempt event store

**Files:**

- Create: `collab/tools/lib/model_observability.py`
- Test: `collab/tests/test_model_observability.py`
- Update: `collab/tools/lib/llm_gateway.py`
- Update: `collab/tests/test_gateway_contract.py`

**Red tests:**

- appending queued/starting/connecting/generating/completed events reduces to one ordered attempt without duplication;
- a second event ID with conflicting immutable content fails closed;
- started is visible before a blocked opener returns;
- timeout, cancellation, retry, parse failure, and telemetry failure remain terminally explicit;
- reducer marks illegal transitions as conflicts instead of guessing;
- redaction rejects prompt/output/private-reasoning fields.

**Implementation:**

- model the lifecycle as an enum plus immutable event dataclass;
- append under the existing collab lock and fsync before transport;
- reduce by `(run_uid, attempt_id)` with deterministic event ordering;
- emit persistence-failure health to a separate atomic health record when append cannot complete;
- retain legacy `model-calls.jsonl` terminal records during migration, derived from v2 terminal events.

**Verify:**

```powershell
uv run --locked pytest collab/tests/test_model_observability.py collab/tests/test_gateway_contract.py -q
uv run --locked ruff check collab/tools/lib/model_observability.py collab/tools/lib/llm_gateway.py collab/tests/test_model_observability.py collab/tests/test_gateway_contract.py
uv run --locked pyright collab/tools/lib/model_observability.py collab/tools/lib/llm_gateway.py
```

### Task 2: Preserve the actual run identity through every dispatcher

**Files:**

- Update: `collab/tools/lib/autopilot.py`
- Update: `collab/tools/lib/lanes.py`
- Update: `collab/tests/test_autopilot.py`
- Update: `collab/tests/test_lanes.py`
- Update: `collab/tests/test_lanes_candidate.py`

**Red tests:**

- builder/reviewer subprocess environment contains the minted `run_uid`, role, handoff, candidate, and attempt IDs;
- breaker/verifier lane dispatch carries the same identifiers;
- retries keep request lineage while minting a new attempt ID;
- no repository slug can satisfy the run UID assertion.

**Implementation:**

- replace the ambiguous local `rid` name with `repo_id`;
- mint `run_uid` before constructing any dispatch environment;
- pass a typed correlation mapping to `_dispatch_seat` and `lanes._dispatch`;
- create the attempt event before subprocess creation so pre-HTTP failures remain visible.

### Task 3: Add read-only Langfuse reconciliation

**Files:**

- Create: `collab/tools/lib/telemetry_reconcile.py`
- Create: `collab/tools/reconcile-telemetry.py`
- Test: `collab/tests/test_telemetry_reconcile.py`
- Update: `collab/tools/lib/dashboard_core.py`
- Update: `collab/tests/test_dashboard.py`

**Red tests:**

- bounded Observations API v2 pagination joins by app `request_id` without loading input/output fields;
- a match records trace/observation IDs and `verified` provenance;
- no match after the configured export grace period records `missing` with an explicit reason;
- API/auth/timeout/parse/flush uncertainty records `verification_failed`, never healthy;
- retries correlate independently and preserve parent request lineage;
- dashboard health is degraded/unknown until every completed in-scope attempt is verified or explicitly failed.

**Implementation:**

- use backend-only Langfuse credentials and the official bounded v2 observations endpoint;
- request only core/basic/time/metadata/model/usage/trace-context fields;
- never emit a second generation or wrap the LiteLLM call with duplicate generation telemetry;
- persist reconciliation events in the same attempt stream;
- expose observation/trace deep links only when host and IDs are verified.

### Task 4: Remove the Haiku direct-provider bypass

**Repositories/files:**

- Gateway repository: `config/llm/model-aliases.yaml`
- Gateway repository: `infra/llm-gateway/litellm-config.yaml`
- Gateway repository tests that enforce alias equality and provider matrix
- This repository: `collab/seats.example.json`
- This repository: `collab/tools/validate-seats.py`
- This repository tests: `collab/tests/test_gateway_contract.py` and seat validation tests

**Implementation:**

- add the exact app-consumed `haiku-4.5` gateway alias mapped to the existing Anthropic Haiku provider ID;
- route the breaker through `openai-repo-seat.py` like every other in-scope model;
- reject in-scope direct-provider commands in seat validation unless a role is explicitly declared out of telemetry scope;
- keep provider keys in the gateway only.

**Live OBS-001 proof:** one call each to GPT, Grok, Gemini, and Haiku with a unique run UID; reconcile app attempt count, LiteLLM spend count, and Langfuse generation count; archive the redacted receipt IDs and completeness verdict.

---

## Candidate OBS-002 — immutable run plan and round decisions

### Task 5: Define the run plan and roster before execution

**Files:**

- Create: `collab/tools/lib/run_plan.py`
- Test: `collab/tests/test_run_plan.py`
- Update: `collab/tools/lib/autopilot.py`
- Update: `collab/tools/lib/dashboard_core.py`
- Update: `collab/tests/test_autopilot.py`
- Update: `collab/tests/test_dashboard.py`

The immutable plan records objective, maximum/minimum/planned/required rounds, early-stop and force-continue rules, eligible models by role, rotation/retry/concurrency strategy, budgets, evaluator version, revision policy, repeat policy, and whether every model is guaranteed an attempt. The roster records configured, selected, queued, invoked, gateway-reached, provider-returned, telemetry-reconciled, evaluated, skipped, disabled, and failed-before-invocation states.

### Task 6: Persist every continue/stop decision

**Files:**

- Create: `collab/tools/lib/round_decision.py`
- Test: `collab/tests/test_round_decision.py`
- Update: `collab/tools/lib/autopilot.py`
- Update: `collab/tools/lib/run_budget.py`
- Update: `collab/tools/lib/run_history.py`

Table-driven tests cover every required stop reason. Each decision records rules considered, positive and negative results, inputs, unresolved requirements, viable models, remaining budget, evidence links, evaluator version, and one authoritative reason. The observed Haiku timeout scenario must reduce to `required_dependency_unavailable`, not `round_cap_reached`.

### Task 7: Replace misleading round labels

**Files:**

- Update: `collab/tools/lib/dashboard_web.py`
- Update: `collab/tools/lib/dashboard_tui.py`
- Update: `collab/tests/test_dashboard_render_contract.py`
- Update: `collab/tests/test_dashboard.py`

Render maximum work attempts, actual work attempts, actor turns, verification calls, provider attempts, and explicit round boundaries separately. Plain-language stop summaries must be deterministic projections from the persisted decision record.

---

## Candidate OBS-003 — sealed archive and replay parity

### Task 8: Seal a complete run manifest

**Files:**

- Create: `collab/tools/lib/run_manifest.py`
- Test: `collab/tests/test_run_manifest.py`
- Update: `collab/tools/lib/run_history.py`
- Update: `collab/tests/test_run_history.py`

The manifest inventories hashes and schema versions for plan, roster, model events, run events, decisions, candidates, assessments, validations, dispositions, requirements, narrative, summary, telemetry, and health. Completion writes artifacts atomically, then writes the manifest last. Missing/altered artifacts fail closed. Interrupted runs receive a recoverable partial manifest with explicit gaps.

### Task 9: Make live and replay use the same projection

**Files:**

- Create: `collab/tools/lib/run_projection.py`
- Test: `collab/tests/test_run_projection.py`
- Update: `collab/tools/lib/dashboard_core.py`
- Update: `collab/tests/test_run_history.py`
- Update: `collab/tests/test_dashboard_stream.py`

Snapshot and run detail call the same pure reducer. Tests cover restart, dashboard initial connection after completion, torn lines, duplicate event IDs, reconnect cursors, more than 200 events, and explicit retention/pruning receipts.

---

## Candidate OBS-004 — coordinated human-forward dashboard views

### Task 10: Build evidence-aware narrative projections

**Files:**

- Create: `collab/tools/lib/operator_narrative.py`
- Test: `collab/tests/test_operator_narrative.py`
- Update: `collab/tools/lib/narrative.py`

Every entry provides actor, action, object, reason, consequence, next action, operator action, stable IDs, evidence links, source type, timestamp, producer, and confidence. A linter rejects generic summaries without meaningful fields.

### Task 11: Render four coordinated views

**Files:**

- Update: `collab/tools/lib/dashboard_web.py`
- Update: `collab/tests/test_dashboard_render_contract.py`
- Update: `collab/tests/test_dashboard_stream.py`

Default operator timeline; persistent per-model activity roster; validation/quality workspace; expandable technical diagnostics. Use tabs/landmarks, keyboard operation, text labels and icons in addition to color, stable deep links, elapsed/last-activity times, bounded lists, filtering, and pagination/virtualization.

---

## Candidate OBS-005 — candidates, quality, requirements, and dispositions

### Task 12: Persist immutable candidate lineage and ownership

**Files:**

- Create: `collab/tools/lib/candidate_evidence.py`
- Test: `collab/tests/test_candidate_evidence.py`
- Update: `collab/tools/lib/candidate_assessment.py`
- Update: `collab/tests/test_candidate_assessment.py`

Record parent/incorporated candidates, producer, prompt/task version, base commit/worktree state, files/patches/tools, evaluator feedback, revisions, final artifact/commit, and current disposition.

### Task 13: Add validation, test-quality, and requirement evidence

**Files:**

- Create: `collab/tools/lib/quality_evidence.py`
- Create: `collab/tools/lib/requirements_matrix.py`
- Test: `collab/tests/test_quality_evidence.py`
- Test: `collab/tests/test_requirements_matrix.py`

Represent automated checks separately from evaluator judgment, model self-report, and human decisions. Capture baseline deltas, separate quality dimensions, uncertainty, gaps, evidence producer/version/artifact, and whether a test proves changed behavior, would fail before the fix, exercises degraded modes, avoids over-mocking, and detects targeted negative variants.

### Task 14: Explain acceptance, rejection, supersession, and disagreement

**Files:**

- Create: `collab/tools/lib/candidate_disposition.py`
- Test: `collab/tests/test_candidate_disposition.py`
- Update: `collab/tools/lib/dashboard_web.py`

Disposition records contain executive explanation, evidence, impact, remediation, retry/finality, useful retained work, alternatives, weaknesses, unavailable evidence, confidence, evaluator disagreements, resolution, and human-review triggers. Acceptance requires complete critical requirements and a named oracle; warnings remain visible.

---

## Candidate OBS-006 — scale, failure, accessibility, and real operator proof

### Task 15: Add failure injection and performance/accessibility budgets

**Files:**

- Create: `collab/tests/test_dashboard_failure_modes.py`
- Create: `collab/tests/test_dashboard_scale.py`
- Create/update browser test assets under the repository’s existing dashboard test convention
- Update: `collab/tools/lib/dashboard_web.py`

Cover many concurrent models, long streams, large histories/diffs, browser sleep/reconnect, bounded server memory, telemetry retry queues, delayed/missing callbacks, persistence failure, cancellation, timeouts, and no duplicated events. Define measured local targets for initial load, update delay, reconnect, and 10k-event navigation.

### Task 16: Execute the representative four-model run

**Artifacts:**

- Create: `docs/evidence/dashboard-human-observability-four-model-<date>.md`
- Create: redacted machine-readable reconciliation receipt under `docs/evidence/`
- Create: final browser screenshots only as secondary evidence

The run must include GPT-5.6 Luna, Grok, Gemini Flash, and Haiku through LiteLLM; candidate production, evaluation, at least one rejection with failed evidence, an accepted/conditional candidate, a maximum greater than actual work attempts, telemetry reconciliation, dashboard restart, and SSE reconnect.

### Task 17: Formal operator usability proof

A representative operator must answer the specified during/after questions using only the dashboard. Record answers, time-to-answer, incorrect/unknown answers, evidence links used, accessibility observations, and remaining gaps. Any question requiring raw JSON or source inspection keeps the goal open.

## Final verification

```powershell
uv run --locked python scripts/verify.py --fail-fast
uv run --locked pytest collab/tests -q
uv run --locked ruff check src tests collab/tools/lib collab/tests scripts
uv run --locked pyright
git diff --check
```

Completion additionally requires the live four-model receipt, Langfuse reconciliation, replay parity, browser proof, and operator scenario record. Unit tests alone cannot close the goal.
