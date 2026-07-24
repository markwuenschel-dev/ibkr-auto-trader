# Dashboard operator-usability proof — 2026-07-22

## Verdict

The operator experience is **partially proven, not completion-ready**.

An executed Chromium/Playwright run proved keyboard navigation, four-model visibility,
quality and rejection evidence, health labels, reload persistence, and reconnect without
duplicate model cards. The corrected fixture and backend projection also prove the exact
two-of-three early-stop contract, honest terminal stuck state, and sealed human run-summary
data. The latest combined Chromium run passed both operator/replay and reconnect scenarios.

The real gateway receipt accounts for all four required aliases through LiteLLM and
Langfuse. GPT, Gemini, and Haiku succeeded; the fresh Grok call reached LiteLLM and xAI but
xAI rejected it for exhausted credits or a monthly spend limit. Consequently, this is not a
fresh successful four-provider orchestration proof.

No prompt, completion, private reasoning, provider credential, raw request identifier, or
raw trace identifier is reproduced here.

## Proof surfaces and boundaries

| Surface | Result | What it proves | What it does not prove |
|---|---|---|---|
| Executed Chromium test | 2 tests passed in 2.6 seconds | Default operator view, honest terminal state, keyboard-only tab navigation, four model cards, quality/rejection evidence, sealed replay, reload persistence, SSE reconnect, no duplicate attempt cards | It is deterministic fixture evidence, not provider execution or independent human-subject evidence |
| Corrected fixture runtime contract | Passed | Terminal status; round 2 of maximum 3; exact early-stop decision; four streamed, routed, correlated model projections | It is deterministic fixture data, not provider execution |
| Latest Playwright source | Chromium passed | The test opens sealed history and asserts the human summary, accepted/rejected candidates, terminal state, two-of-three accounting, and reconnect identity | It does not replace a real four-provider candidate run |
| Real four-alias gateway receipt | Partial pass | Four LiteLLM attempts and four distinct Langfuse observations; successful GPT, Gemini, and Haiku; explicit Grok provider failure | Fresh successful xAI/Grok response and a real candidate/evaluation run |
| Sealed replay contract tests | Passed | Immutable manifest, archive-only reconstruction, retained stop decision, dispositions, evidence, and human summary | Real-provider replay remains pending |

## Operator scenario

The representative scenario has four configured roles:

- GPT-5.6 Luna produces a candidate.
- Grok 4.5 evaluates it.
- Gemini 3.5 Flash probes the candidate for failures.
- Haiku 4.5 verifies retained findings and validation evidence.

Candidate `candidate-rejected` fails the keyboard persistence requirement and is rejected
with evidence, impact, remediation, retained work, and evaluator disagreement. Its descendant
`candidate-accepted` passes the named browser oracle and the critical keyboard requirement.
The run stops after round 2 of a maximum 3 because the accepted candidate met completion
criteria and no unresolved requirements remained. The maximum was an allowance, not a
promise to execute three rounds.

## During-execution questions

| Operator question | Dashboard answer | View/evidence | Proof status |
|---|---|---|---|
| What is happening right now? | Stage, current work attempt, latest meaningful event, and terminal decision are presented separately. | Operator timeline and hero | Latest browser run proves the honest terminal wording |
| Which model is doing it? | Each attempt has its own role/model card; concurrent attempts are not collapsed into one scalar. | Model activity | Browser proven with four distinct cards |
| Why was that model selected? | The roster names the configured role, selection reason, and assigned task. | Run plan and model activity | Projection, render contract, and browser proven |
| Is it making progress? | Lifecycle, elapsed time, last safe activity, response-started phase, chunks, and completion timing are shown without model reasoning. | Model activity | Provider-specific stream contract and browser presentation passed |
| Is anything stuck? | Timeout, cancellation, retry, persistence, gateway, Langfuse, freshness, and stream health are distinct states. | Model activity and diagnostics | Failure-injection and render-contract tests passed |
| What will happen next? | Each narrative event includes consequence, next action, and any required operator action. | Operator timeline | Projection/render-contract proven |
| Why has the next round not started? | “Stopped after completed round 2 of maximum 3” because the accepted result met completion criteria; every competing stop rule and its input/result remain inspectable. | Operator timeline and decision detail | Corrected runtime projection and browser replay proven |

## After-execution questions

| Operator question | Dashboard answer | View/evidence | Proof status |
|---|---|---|---|
| What changed? | Candidate lineage names parent, task version, base state, changed files, tools, incorporated work, and final disposition. | Validation and quality; sealed run detail | Render/replay contracts and browser replay passed |
| Which candidate won? | `candidate-accepted`. | Quality view and human run summary | Browser quality view proven; sealed summary contract passed |
| Why did it win? | It passed the named browser oracle and all critical requirements; the disposition retains alternatives, weaknesses, confidence, and unavailable evidence. | Acceptance disposition | Render/replay contracts passed |
| Why did the other candidate lose? | Keyboard persistence failed; accepting it would make keyboard-only operators lose context after reload; it is retryable after durable selection/focus behavior is fixed. | Rejection disposition | Browser rejection evidence and sealed replay passed |
| What evidence says the code works? | Automated validation, requirement evidence, provenance, producer/version, test-quality flags, gaps, and uncertainty are shown separately from evaluator judgment and model claims. | Validation and quality | Browser and contract tests passed |
| Which requirements remain uncertain? | The UI must report fresh xAI provider success as missing evidence, not as green. | Requirement matrix and human summary | Summary contract passed; real receipt confirms the gap |
| Did every model call reach LiteLLM? | In the fixed real proof, all four aliases reached the canonical gateway. Grok then failed at xAI with HTTP 403. | Model activity and real gateway receipt | Proven for four minimal real calls, not for a full candidate run |
| Did every model call reach Langfuse? | All four fixed real attempts matched distinct Langfuse observations, including the failed Grok generation. | Telemetry reconciliation and real receipt | Proven for four minimal real calls |
| Why did the run end? | Representative fixture: accepted result met completion criteria after round 2 of maximum 3. Original observed run: baseline breaker timed out and the old archive incorrectly flattened the outcome to `capped`. | Decision record; original reproduction artifact | Structured new case and original diagnosis proven |
| Does a human need to do anything? | Yes: restore xAI credits/monthly capacity and rerun a real four-provider candidate/evaluation scenario. | Human actions and missing evidence | Proven open actions; completion remains blocked |

## Performance and accessibility evidence

- The executed browser test required initial connection in less than 3 seconds and reconnect
  in less than 35 seconds; both assertions passed.
- Keyboard-only `ArrowRight`, `Home`, and `End` navigation moved focus and selection across
  the four coordinated views.
- Reload restored the selected view.
- Every button, input, and select in the exercised page had an accessible name.
- Reconnect retained exactly four rendered attempt cards.
- Backend scale tests project 10,000 events under explicit read/projection/payload budgets,
  return bounded windows with totals and truncation metadata, and bound in-memory snapshot
  retention.
- The refreshed connected observability suite passed 283 tests on 2026-07-24, including the
  repository-aware adapter's redacted tool-lifecycle projection. The focused lineage,
  gateway, persistence-health, replay, and summary contracts also passed.
- Ruff passed across `collab` and `scripts`; the changed observability modules and fixture
  scripts passed Pyright with zero errors; dashboard Oxlint and Oxfmt passed; dashboard
  Vitest passed 3 tests using its supported runner config loader.
- The repository-standard verifier passed its lock check, 190 core tests, core Ruff and
  Pyright with zero errors, 832 collab tests with 6 skips and 23 subtests, dashboard Vitest,
  Oxlint, Oxfmt, and the Next production build. Its declared standing omission remains
  collab-wide Pyright; broker/integration tests and Playwright are intentionally outside it.
- The separately executed latest Playwright slice passed 2 Chromium tests in 2.6 seconds
  (operator/replay and reconnect); the unrelated operational-fixture scenario was skipped
  because this server intentionally exposes the observability fixture.
- The gateway alias/config repository passed its full hermetic pytest suite, Ruff, Oxfmt,
  strict project Mypy, live config validation, and every canonical/root Compose combination.
  Its GitHub CI was all green before PR 14 merged.
- Candidate producer lineage now references the durable orchestration execution UUID rather
  than a synthetic actor-turn label. Orchestration and gateway model-event writes both use a
  health-aware append path that records redacted durable persistence failure state.
- Every expected roster entry now has an explicit terminal disposition once the final stop
  decision exists. Uninvoked roles become reasoned skips; direct-provider bypass, missing
  evaluation, and missing telemetry keep the human summary incomplete. Sealed run archival
  has its own health dimension, separate from lifecycle-history persistence.
- Candidate, requirement, validation, disposition, and round-decision writes now share one
  idempotent health-aware `run-events` append contract. Its failure sidecar is run-scoped,
  archived, included in the sealed human summary, and displayed separately from model-call
  and run-archive persistence.
- `openai-repo-seat.py` now emits run-correlated `tool_started`, `tool_completed`, and
  `tool_failed` lifecycle evidence before and after local tool dispatch. The durable records
  retain only tool name, call identifier, step, phase, and result status; arguments, returned
  content, and private paths are excluded. Live and sealed views preserve a compact summary.

Screenshot from the executed browser slice:
`docs/evidence/dashboard-human-observability-runtime-2026-07-22.png`.

## Remaining completion gates

1. Restore xAI provider capacity and execute a real four-model candidate/evaluation run.
2. Reconcile every attempt from orchestration through LiteLLM, provider outcome, Langfuse,
   candidate/evaluation evidence, and final disposition under one run UID.
3. Have an independent representative operator answer the question table without source or
   raw JSON if human-subject evidence, rather than scripted usability acceptance, is required.
4. Execute and reconcile a real tool-calling run through `openai-repo-seat.py` to prove the
   tested adapter contract at runtime. Any other opaque CLI adapter that claims internal tool
   use must implement the same explicit lifecycle contract before its tool activity can be
   considered end-to-end observable.

Until these gates close, the correct readiness verdict is **not ready**.
