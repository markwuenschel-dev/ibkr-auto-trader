# Dashboard human-observability runtime evidence — 2026-07-22

## Verdict

The existing dashboard is operationally useful but does not satisfy the human-observability goal. The observed run did use multiple gateway-backed models, and every app-recorded GPT/Grok request was exported to Langfuse. The failure is principally loss of run identity, incomplete lifecycle retention, a direct-provider bypass for Haiku, and UI language that compresses distinct work attempts, model requests, verification calls, and terminal reasons into “rounds.”

This document contains no prompts, model outputs, API keys, or provider credentials.

## Fixed observation scope

- Repository: `ibkr-auto-trader`
- Archived run UID: `20260721T234615Z-140256`
- Run window: `2026-07-21T23:46:15Z` through `2026-07-21T23:52:26Z`
- Git commit: `e55384b`
- LiteLLM runtime: `ghcr.io/berriai/litellm:v1.92.0`
- Langfuse query window: `2026-07-21T23:46:00Z` through `2026-07-21T23:53:00Z`

## Reproduction oracle

Run the read-only reproducer against the retained run:

```powershell
uv run --locked python scripts/repro_dashboard_observability.py `
  --collab-root C:\Users\Nalakram\Documents\GitHub\ibkr-auto-trader\autopilot `
  --run-uid 20260721T234615Z-140256
```

Expected current result: exit code `2`, because at least these contracts are false:

- the archive preserves the final budget;
- run detail exposes model attempts;
- attempts carry the archived run UID;
- every planned model has an attempt;
- the event stream contains model lifecycle events.

## What actually happened

### Orchestration record

The run archive reports:

- phase `capped`;
- two actor turns against a configured maximum of three work attempts;
- one builder turn and one reviewer turn;
- final status error `035 paused (infrastructure_blocked); awaiting human`;
- budget consumption of one work attempt, two verification passes, three verification calls, five orchestration-level model calls, and two actor turns.

Therefore “2 / 3 rounds” is not a faithful stop explanation. The work-attempt limit was not exhausted. The run stopped because the baseline breaker command timed out after 180 seconds, and the terminal archive flattened that state to `capped`.

### Execution roster

| Planned role | Configured model | Observed outcome |
|---|---|---|
| Builder | `gpt-5.6-luna` | Invoked through LiteLLM; 10 app-recorded successful HTTP attempts |
| Reviewer | `grok-4.5` | Invoked through LiteLLM; 7 app-recorded successful HTTP attempts |
| Baseline breaker | `haiku-4.5` | Direct `claude -p` command in the local runtime config; timed out; never entered LiteLLM |
| Baseline verifier | `gemini-3.5-flash` | Planned but not invoked because baseline breaker failed first |
| High-risk breaker | `gpt-5.6-luna` | Invoked; contributed to the GPT aggregate |
| High-risk verifier | `grok-4.5` | Invoked; contributed to the Grok aggregate |

The checked-in example config instead provides an `anthropic-general` gateway alias. The local runtime therefore drifted from the repository example at the exact seam the dashboard was expected to prove.

### LiteLLM and Langfuse reconciliation

Read-only LiteLLM `LiteLLM_SpendLogs` aggregation for the fixed window returned 28 successful calls:

- 16 `openai/gpt-5.6-luna`;
- 12 `xai/grok-4.5`;
- zero Gemini or Anthropic calls in that window.

Seventeen rows carried `session_id = ibkr-auto-trader`, exactly matching the app ledger’s 10 GPT plus 7 Grok records. Eleven additional rows used UUID session IDs. All rows retained service, environment, feature, and model-alias tags, but none carried the archived run UID.

The Langfuse Observations API v2 returned 28 generation observations in the same window:

- all 28 have model, usage, cost, total latency, and first-token timing;
- 17 have `sessionId = ibkr-auto-trader` and 11 have no Langfuse session ID;
- all 17 local app `request_id` values were found in Langfuse requester metadata;
- zero local app request IDs were missing from Langfuse.

Thus the provider traces were exported. The current product cannot prove that fact because it does not perform or retain the join, and its health record equates a successful local JSONL append with gateway health while leaving Langfuse `unverified` unless a separate manual verification record is written.

## Retention and replay gaps

The archived run contains only:

- `events.jsonl`;
- `run.json`;
- `status.json`.

`run.json` omits the final budget, model attempts, Langfuse observation IDs, assessment records, candidate lineage, requirement coverage, and complete dispositions. `dashboard_core.run_detail()` exposes only `summary`, `events`, and `lanes`. The event archive contains eight high-level orchestration events and no queued, gateway-accepted, first-token, completed, failed, cancelled, retried, or telemetry-verified model lifecycle events.

## Baseline verification

Before diagnostic artifacts were added, the isolated worktree passed the authoritative Python baseline:

```text
uv run --locked python scripts/verify.py --python-only --fail-fast
190 core tests passed, 1 deselected
Ruff passed
Pyright: 0 errors
723 collab tests passed, 6 skipped, 23 subtests passed
PARTIAL PASS: dashboard omitted by --python-only
```

## Evidence boundaries

- Docker log searches for the application request IDs/model patterns returned no hits. Per repository policy, that proves only that those patterns were not found in the searched log slice; absence is not confirmed.
- The 11 UUID-session LiteLLM calls share the same app tags and run window, but this evidence does not yet establish which internal adapter lifecycle produced them.
- Langfuse was queried read-only with bounded timestamps and field groups that excluded inputs and outputs.
