# Dashboard Operational State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the collab dashboard a complete, trustworthy, live operational view with canonical per-handoff state/history, explicit health, gateway-only adapter traffic, and correlated model telemetry.

**Architecture:** Add one deep `operational_state` module that owns the typed effective-state contract, precedence, append-only lifecycle events, replay, reconciliation, and source health. Migrate lifecycle producers to emit through that module, make `dashboard_core.snapshot()` project only the canonical contract, stream authoritative snapshots over resumable SSE, and centralize both OpenAI-shaped adapters on a gateway-only transport that writes bounded operational telemetry. Existing board directories, leases, escalation artifacts, requests, and run archives remain authoritative source records; disagreement is retained and surfaced as a conflict instead of flattened.

**Tech Stack:** Python 3.14 stdlib, dataclasses/enums/JSONL, existing `collab_common` atomic locking, `ThreadingHTTPServer` SSE, browser DOM APIs, pytest, repository `scripts/verify.py`, Playwright.

## Global Constraints

- Preserve paper-trading and existing access-control boundaries; this migration must not touch broker execution behavior.
- Do not create compatibility wrappers or a second shadow state contract.
- Never classify missing, malformed, stale, or conflicting evidence as healthy, idle, completed, or non-escalated.
- Do not expose credentials, authorization headers, full prompts, private payloads, or unbounded raw logs.
- LiteLLM application traffic uses `LITELLM_BASE_URL` plus `LITELLM_VIRTUAL_KEY`; no provider-key or direct-provider fallback.
- Langfuse uses the sibling gateway's classic success/failure callback and native-dimension metadata conventions; app code does not dual-write generations.
- Historical corrections are new events; retained events are never rewritten.
- The measurable local update target is p95 <= 2 seconds from committed source change to applied dashboard snapshot.
- UI status must not rely on color alone.

---

### Task 1: Canonical operational-state and history contract

**Files:**
- Create: `collab/tools/lib/operational_state.py`
- Create: `collab/tests/test_operational_state.py`
- Modify: `collab/tools/lib/trace.py`
- Modify: `collab/tools/lib/handoff_core.py`
- Modify: `collab/tools/lib/escalation.py`
- Modify: `collab/tools/lib/operator_requests.py`
- Modify: `collab/tools/lib/autopilot.py`

**Interfaces:**
- Consumes: authoritative board directory state, live lease/status, control flags, escalation records, operator requests, transition records, and run/event identifiers.
- Produces: `OperationalState`, `OperationalEvent`, `OperationalItem`, `append_event(collab, event)`, `read_history(collab, entity_id, *, after=None, limit=100)`, `reduce_history(events)`, and `reconcile_item(collab, handoff, *, now=None)`.

- [ ] **Step 1: Write failing contract/replay tests** for all 14 required states, stable event IDs, duplicate delivery, out-of-order delivery, invalid transitions, late corrections, and deterministic replay.
- [ ] **Step 2: Run `uv run pytest -q collab/tests/test_operational_state.py`** and capture the expected import/contract failures.
- [ ] **Step 3: Implement the typed contract** with `OperationalState(str, Enum)`, frozen event/item dataclasses, schema `1.0`, explicit actor/run/trace/escalation/action fields, and precedence:

```python
STATE_PRECEDENCE = {
    OperationalState.COMPLETED: 140,
    OperationalState.CANCELLED: 130,
    OperationalState.SUPERSEDED: 120,
    OperationalState.ESCALATED: 110,
    OperationalState.FAILED: 100,
    OperationalState.BLOCKED: 90,
    OperationalState.CAPPED: 80,
    OperationalState.PAUSED: 70,
    OperationalState.RETRYING: 60,
    OperationalState.RUNNING: 50,
    OperationalState.AWAITING: 45,
    OperationalState.PARKED: 40,
    OperationalState.CLAIMED: 30,
    OperationalState.QUEUED: 20,
}
```

- [ ] **Step 4: Implement append/replay** under `autopilot/lifecycle/<hid>.jsonl` using `collab_lock`, event-ID deduplication, event-time ordering with ingestion-time/event-ID tie breaks, rejection counters, cursor pagination, and explicit reconciliation events.
- [ ] **Step 5: Migrate lifecycle producers directly** so create/claim/reclaim/done/archive, escalation write/clear, request write/consume, and meaningful autopilot phase changes emit canonical events after their authoritative write commits.
- [ ] **Step 6: Add legacy reconstruction** that reads existing source records without rewriting them, emits one explicit `reconciliation` event when history starts, and reports malformed/contradictory evidence as conflicts.
- [ ] **Step 7: Run the focused tests** and prove replay is idempotent and the original escalation record reduces to `escalated` with `parked` in conditions and a required operator action.

### Task 2: Canonical snapshot, detail, health, and migration

**Files:**
- Modify: `collab/tools/lib/dashboard_core.py`
- Modify: `collab/tools/lib/run_history.py`
- Modify: `collab/tests/test_dashboard.py`
- Modify: `collab/tests/test_run_history.py`

**Interfaces:**
- Consumes: `operational_state.reconcile_item()` and paged lifecycle history.
- Produces: snapshot schema `1.0` with `items`, canonical `board` rows, `state_counts`, `health`, `freshness`, `stream`, and detail/history views.

- [ ] **Step 1: Add failing snapshot tests** for live escalation, parked/no-run, every canonical state, reason/owner/timestamp/action fields, malformed escalation, conflicts, stale sources, and empty-active-run views that still contain parked/escalated work.
- [ ] **Step 2: Run the focused tests** and record current failures, including that the July 21 patch exposes only `escalated`/`escalation_reason` and not the full contract.
- [ ] **Step 3: Replace `_row`'s ad-hoc state projection** with `OperationalItem.to_dict()` and make `open_handoffs()` reuse the already-built board rather than rebuilding and rereading all sources.
- [ ] **Step 4: Add separate health records** for source reads, freshness, stream transport, history persistence, gateway reachability, Langfuse export evidence, reconciliation conflicts, rejected/dropped events, and schema compatibility. Health values are `healthy|degraded|unavailable|unknown`, each with timestamp and reason.
- [ ] **Step 5: Add paged `operational_detail(collab, hid, cursor=None, limit=100)`** returning canonical item, history page, source evidence summary, run/correlation identifiers, and redacted model telemetry.
- [ ] **Step 6: Stop treating the per-run archive as lifecycle history**; retain existing 25-run UI history but document it as run evidence while canonical item history remains independent and append-only.
- [ ] **Step 7: Run dashboard/run-history focused tests.**

### Task 3: Resumable live transport and reconciliation

**Files:**
- Modify: `collab/tools/lib/dashboard_web.py`
- Create: `collab/tests/test_dashboard_stream.py`
- Modify: `collab/tests/test_run_history.py`

**Interfaces:**
- Consumes: authoritative `dashboard_core.snapshot()`.
- Produces: `GET /api/stream` SSE with `instance_id`, monotonic sequence, replay ring, gap reconciliation, keepalive, and full-snapshot events; `GET /api/operational` for paged detail/history.

- [ ] **Step 1: Add failing transport tests** for initial snapshot, duplicate sequence, out-of-order sequence, cursor resume, missed-event gap, reconnect snapshot, stale/new ordering, and bounded connection teardown.
- [ ] **Step 2: Run `uv run pytest -q collab/tests/test_dashboard_stream.py`.**
- [ ] **Step 3: Implement an in-process bounded stream broker** that hashes snapshots, increments only on material change, keeps 512 events, sends `retry: 1000`, replays an available `Last-Event-ID`, and emits `reconcile` when the cursor is missing or from another server instance.
- [ ] **Step 4: Implement browser stream state** with explicit `connected|reconnecting|stale|disconnected`, a 1/2/4/8/16/30-second bounded retry schedule, sequence rejection, gap-triggered `/api/state` reconciliation, and a latest-applied timestamp.
- [ ] **Step 5: Keep a 30-second bounded snapshot poll only as a reconciliation safety net**, never as the primary update path, and suppress overlapping requests.
- [ ] **Step 6: Add a timing test** that commits a fixture state transition and asserts it reaches the client-facing stream within 2 seconds locally.
- [ ] **Step 7: Run stream and web integration tests.**

### Task 4: Complete operator UI

**Files:**
- Modify: `collab/tools/lib/dashboard_web.py`
- Modify: `collab/tools/lib/dashboard_tui.py`
- Modify: `collab/tests/test_dashboard.py`
- Create: `collab/tests/test_dashboard_render_contract.py`

**Interfaces:**
- Consumes: snapshot schema `1.0` canonical rows and operational detail endpoint.
- Produces: accessible state chips, counts/filters, health/freshness panels, canonical board/open-item rows, detail/history panels, and explicit empty/degraded states.

- [ ] **Step 1: Add a failing render-contract test** that parses the embedded page and proves every canonical label, reason, owner, last transition, freshness, required action, escalation severity/reason, run/trace identifiers, and non-color status text has a rendering path.
- [ ] **Step 2: Run the render test** and capture that current `renderPipe()` ignores the escalation fields already present in rows.
- [ ] **Step 3: Render summary counts and filters** for every state without removing parked/escalated work when no run is active.
- [ ] **Step 4: Render every active/open row** with effective state, conditions, reason, owner, transition time, freshness, and required action; show malformed/conflicting source warnings inline.
- [ ] **Step 5: Replace the generic handoff modal** with an operational detail panel containing redacted source evidence, paginated lifecycle timeline, run/correlation IDs, escalation fields, and operator actions.
- [ ] **Step 6: Add the separate health matrix and transport banner**; no aggregate green indicator is allowed.
- [ ] **Step 7: Mirror the essential state/health information in the TUI** and run focused tests.

### Task 5: Gateway-only model transport and complete telemetry

**Files:**
- Create: `collab/tools/lib/llm_gateway.py`
- Modify: `collab/tools/adapters/openai-compatible-seat.py`
- Modify: `collab/tools/adapters/openai-repo-seat.py`
- Modify: `collab/tools/lib/autopilot.py`
- Modify: `collab/tools/lib/dashboard_core.py`
- Modify: `collab/tests/test_openai_adapter.py`
- Modify: `collab/tests/test_openai_repo_seat.py`
- Create: `collab/tests/test_gateway_contract.py`

**Interfaces:**
- Consumes: sibling gateway contract: `/v1`, virtual key, declared alias, classic Langfuse callback metadata conventions.
- Produces: `GatewayConfig.from_env()`, `request_metadata()`, `post_json()`, error classification, and append-only redacted `autopilot/model-calls.jsonl` records correlated to handoff/run/seat/request/trace.

- [ ] **Step 1: Add failing gateway tests** for success, timeout, retry classification, cancellation, HTTP/provider/gateway/parsing failure, missing key/base, direct-provider base/key rejection, Responses fallback, tokens/cost, redaction, Langfuse reserved metadata, and telemetry append failure.
- [ ] **Step 2: Add a failing repository check** that rejects provider API hosts and provider-key defaults in `collab/tools/adapters` while allowing test fixtures and gateway configuration docs.
- [ ] **Step 3: Implement gateway-only configuration** requiring `LITELLM_BASE_URL` and `LITELLM_VIRTUAL_KEY`, rejecting master/provider keys and known provider hosts, preserving configured timeouts and existing chat/Responses semantics, with no silent provider fallback.
- [ ] **Step 4: Implement the full metadata projection**: UUID `request_id`, service, feature, environment, release, model alias, trace/session/user where present, `trace_name`, `generation_name`, `trace_release`, low-cardinality tags, plus `handoff_id`, `run_uid`, `seat`, `candidate_id`, and escalation ID in ordinary metadata.
- [ ] **Step 5: Emit one telemetry record per HTTP attempt** with requested/actual model, provider if returned, start/end/first-token/total latency where observed, input/output/cached/total tokens, cost, retry, streaming completion/interruption, outcome classification, correlation IDs, environment/release, and `langfuse_export=unverified|verified|rejected`. Unknown values remain null.
- [ ] **Step 6: Redact keys, headers, prompt/completion bodies, secret-shaped metadata, and high-cardinality tags. Telemetry write failure increments a health counter but never changes the model result.**
- [ ] **Step 7: Update dashboard health/model detail projection and run gateway-focused tests.**

### Task 6: Browser, integration, runtime, and documentation proof

**Files:**
- Modify: `dashboard/playwright.config.ts` only if the existing runner cannot target the Python dashboard
- Create: `collab/tests/browser/dashboard_operational.spec.ts` or extend the repository's accepted browser harness
- Modify: `dashboard/README.md`
- Modify: `docs/design/collab-kit-architecture.md`
- Create: `docs/design/dashboard-operational-state.md`
- Create: `docs/evidence/dashboard-operational-state-2026-07-21.md`
- Modify: `README.md` and configuration examples only where the public workflow changes

**Interfaces:**
- Consumes: completed dashboard, gateway, lifecycle, history, and health contracts.
- Produces: automated browser coverage, migration/runbook docs, and retained runtime evidence.

- [ ] **Step 1: Add browser scenarios** for every state, escalation appearing/clearing live, parked with no run, filters/counts, reload/history reconstruction, reconnect/missed events, stale/new ordering, source conflicts, and accessible non-color status.
- [ ] **Step 2: Run unit/contract/integration/browser checks** and repair only evidenced failures.
- [ ] **Step 3: Run the actual Python dashboard against a realistic temporary collab** and capture screenshots/logs showing escalation without refresh, parked/no-run visibility, detail/history after reload, and degraded health.
- [ ] **Step 4: Probe the configured LiteLLM gateway** and make one redacted correlated model request if credentials/services are available; record request, run, handoff, and trace IDs without recording secrets or prompt bodies.
- [ ] **Step 5: Verify Langfuse generation presence through the available Cloud UI/API or record an exact external blocker. Configuration inspection alone is not proof.**
- [ ] **Step 6: Document sources, identifiers, precedence, transitions, schema migration, retention/pagination, SSE recovery, gateway setup, privacy, health interpretation, and troubleshooting.**
- [ ] **Step 7: Run `uv run --locked python scripts/verify.py`, Playwright, integration tests, `git diff --check`, and a secret scan; reread every completion criterion against retained evidence.**
