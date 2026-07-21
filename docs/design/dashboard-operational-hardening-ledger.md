# Dashboard operational hardening ledger

Updated: 2026-07-21

## Verified baseline

- Source commit: `cae451868ca9ac3abe19c2435a8300bb3e6032d8` on clean `main`.
- `uv run --locked python scripts/verify.py`: 190 core tests passed; 690 collab tests passed, 6 skipped, 23 subtests passed; Ruff, Pyright, dashboard Vitest, lint, format, and production build passed.
- Standing omissions reported by the gate: collab type-checking, broker/integration tests, and dashboard Playwright.

## Confirmed bugs and gaps

1. `79b549b` fixed the original snapshot omission only partially: `_row()` now adds `escalated` and `escalation_reason`, and the idle hero renders one escalation. `renderPipe()` still ignores both fields; open/detail/history/count/filter views have no canonical escalation/parked state.
2. The live repo has handoff `035` in `claimed/` and `autopilot/escalations/035.md`; current snapshot reports `escalated=true`, but omits severity, escalation timestamp, owning actor/run, required action, freshness, and conflict/source health.
3. Board directories encode only `pending/claimed/done/archive`; live status, lease, controls, escalation records, requests, close transitions, and run archives encode independent operator facts with no canonical reducer.
4. `logs/events.jsonl` is wiped at each run start; archived run evidence is pruned to 25 directories. Neither is an immutable per-handoff lifecycle history.
5. `read_status()` and several source readers collapse missing, malformed, and unreadable data to `None`, `{}`, or `[]`, making absence indistinguishable from read failure.
6. The web UI polls `/api/state` every 2 seconds, has no cursor, sequence, deduplication, gap detection, replay, or authoritative reconnect reconciliation.
7. `openai-compatible-seat.py` and `openai-repo-seat.py` default to direct provider hosts/keys when no gateway virtual key is present. That is a silent gateway bypass.
8. Both adapters duplicate a partial metadata projection and usage logger. They omit the gateway's Langfuse native-dimension fields, task/run/seat/escalation correlation, completion/output/total tokens, cost, actual model/provider, classified failures, and telemetry-delivery health.

## Requirements discoveries

- Coarse board directory state remains authoritative for file lifecycle; the canonical operational item must retain that source fact rather than replace it with inferred display state.
- Live ownership is authoritative only while the board lease is fresh; `status.json` alone is not liveness.
- An open escalation record is authoritative for escalation/parked disposition even when no run is active.
- A pending operator request is authoritative for required action already filed; consuming it is a subsequent lifecycle event, not deletion of historical truth.
- The sibling gateway at commit `4d2a0bcf12418063259fc03ee3f9fd02d68e000e` defines the app contract: OpenAI-compatible `/v1`, virtual-key-only auth, declared aliases, classic Langfuse success/failure callbacks, and required request attribution.
- Gateway metadata must include native Langfuse projection (`trace_name`, `generation_name`, `trace_release`, low-cardinality tags, optional pseudonymous `trace_user_id`); proxy callback owns generation emission.
- Gateway/Langfuse live proof is credential/service dependent. Until exercised, health must be `unknown` or `unavailable`, never green.

## Suspected bugs to test

- A malformed escalation file is currently swallowed and displayed as non-escalated.
- A newer event followed by an older polled snapshot can regress the current UI because there is no revision comparison.
- Duplicate run/event delivery can duplicate timeline entries because current feed identity is positional.
- A stale `status.json` may still contaminate non-live derived history even though live panels are cleared.
- Usage logging can silently drop telemetry without any dashboard-visible counter.

## Ruled-out leads

- The dashboard is not one application: the Python collab dashboard is the operational board; `dashboard/` is the separate trading telemetry Next.js app. This migration targets the Python collab dashboard and only reuses the existing browser toolchain for proof.
- The source checkout no longer has the pre-`79b549b` data omission; reproduction must distinguish the parent-commit defect from the remaining current UI/contract gap.
- `status.json` is not a durable owner record after lease loss; treating its last `active_seat` as current ownership would reintroduce ghost-run behavior.

## Unrelated technical debt

- The authoritative verification gate reports 372 known collab Pyright findings. This migration will type its new contract and test it but will not clear all pre-existing findings.
- Trading-domain PT-4 through PT-7 work is unrelated to dashboard operational state and remains out of scope.

## External/runtime unknowns

- Whether the local LiteLLM stack is currently running and reachable.
- Whether a valid app virtual key is available to this worktree.
- Whether Langfuse Cloud read credentials or UI access are available for automated generation lookup.
- Whether the pinned proxy currently promotes every ADR-0007 reserved field; the gateway repo labels that live promotion unproven until its pin test and Cloud check pass.
