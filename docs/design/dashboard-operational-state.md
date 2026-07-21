# Collab dashboard operational-state contract

## Purpose and ownership

The local collab mission-control dashboard answers two separate questions without conflating them:

1. **Where is the handoff file?** `pending|claimed|done|archive` remains the CAS/action-routing truth owned
   by `handoff_core`.
2. **What does the operator need to know now?** `OperationalState` owns the effective lifecycle meaning:
   `queued`, `claimed`, `running`, `awaiting`, `paused`, `capped`, `blocked`, `parked`, `escalated`,
   `retrying`, `failed`, `cancelled`, `superseded`, or `completed`.

`collab/tools/lib/operational_state.py` is the single contract. Renderers project it; they do not maintain
their own state tables. Existing board directories, the live lease/status, escalation records, operator
requests, and close-transition records remain authoritative source facts. A conflict is retained and
shown; precedence never deletes evidence.

## Lifecycle history and migration

Each handoff has an append-only schema-`1.0` history at
`<collab>/autopilot/lifecycle/<handoff-id>.jsonl`. Events contain a stable event ID, per-entity sequence,
previous/new state, reason, source, actor, run/correlation/trace identifiers, event and ingestion times,
conditions, escalation metadata, and required action.

- Writers lock per entity and deduplicate by event ID.
- Replay orders by event time, ingestion time, sequence, and event ID; duplicate delivery is idempotent.
- Previous-state mismatches, illegal transitions, ID collisions, rejected records, and future schemas are
  conflicts, not successful guesses.
- Retained events are immutable. A late correction is a new `source=reconciliation` event.
- A legacy handoff with no lifecycle record gets exactly one deterministic reconstruction event derived
  from its retained source facts. Original source records are not rewritten.
- `operational_detail()` pages history by stable sequence cursor (default 100, maximum 1000).

The existing 25-entry run archive remains run evidence. It is not handoff lifecycle history.

## Effective-state precedence

The explicit precedence is: completed, cancelled, superseded, escalated, failed, blocked, capped, paused,
retrying, running, awaiting, parked, claimed, queued. Lower-priority simultaneous facts remain visible as
conditions or conflicts. An open escalation resolves to `escalated` with `parked` in `conditions`; it keeps
reason, severity, timestamp, run ID, and `required_action=retry_or_adopt` even when no driver is live.

## Live transport and recovery

`GET /api/stream` is the primary browser transport. It sends full authoritative snapshots over SSE with a
server `instance_id`, monotonic sequence, stable event ID, and a bounded 512-event replay ring. A client
may resume with `Last-Event-ID` or `?cursor=`. A missing/foreign/future cursor receives a `reconcile` full
snapshot.

The browser rejects duplicate and out-of-order sequences, reconciles gaps through `/api/state`, reconnects
at 1/2/4/8/16/30 seconds, and retains a non-overlapping 30-second snapshot fetch only as a safety net.
Transport state is explicit text: `CONNECTED`, `RECONNECTING`, `STALE`, or `DISCONNECTED`. The local
source-change-to-stream contract is p95 <= 2 seconds; the server checks sources every 500 ms while a stream
is connected.

## Health semantics

Health is a matrix, not one green light: source reads, reconciliation, history persistence, schema
compatibility, freshness, stream, gateway, and Langfuse. Each dimension is
`healthy|degraded|unavailable|unknown` with timestamp and reason. Missing evidence is `unknown`, never
healthy. Malformed/future records degrade or make their specific dimension unavailable without hiding
other usable evidence.

## LiteLLM and Langfuse

Both OpenAI-shaped seat adapters require `LITELLM_BASE_URL` plus `LITELLM_VIRTUAL_KEY`. Provider hosts and
alternate provider/master-key selectors are rejected; there is no direct fallback. The shipped seat
catalog uses gateway aliases for all model traffic while preserving provider-disjoint assurance profiles.

Each request sends the gateway-native Langfuse projection: request ID, service, feature, environment,
release, model alias, `trace_name`, `generation_name`, `trace_release`, low-cardinality tags, and optional
session/pseudonymous-user fields. Handoff, run, seat, candidate, and escalation IDs are ordinary metadata.
The proxy owns generation export through its classic success/failure callback; the application does not
dual-write generations.

Every HTTP attempt appends a bounded, redacted record to `<collab>/autopilot/model-calls.jsonl`: requested
and actual model, provider, timing, tokens, cost, retry/stream completion, classified outcome, correlations,
and `langfuse_export=unverified|verified|rejected`. Keys, headers, prompts, completions, and secret-shaped
error text are never persisted. Telemetry failure updates its health record but never changes the model
result.

## Operator runbook

1. Set `LITELLM_BASE_URL`, `LITELLM_VIRTUAL_KEY`, `SERVICE_NAME`, environment, and release as shown in
   `collab/.env.example`.
2. Start the local dashboard through the existing collab dashboard command.
3. Treat `unknown` as missing proof. Inspect the health dimension and item conflicts before acting.
4. For `escalated + parked`, open the item, read its lifecycle/source evidence, then file Retry or Adopt.
5. If the stream reconnects after a server restart, expect a full reconcile snapshot; sequence restarts are
   scoped by `instance_id`.
6. Use run history for run comparison and lifecycle history for handoff reconstruction; never substitute
   one for the other.
