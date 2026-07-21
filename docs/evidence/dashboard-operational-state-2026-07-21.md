# Dashboard operational-state evidence — 2026-07-21

## Baseline and reproduced defect

- Baseline commit: `cae451868ca9ac3abe19c2435a8300bb3e6032d8`.
- Baseline authoritative verification: 190 core tests passed; Ruff passed; Pyright passed; 690 collab tests
  passed with 6 skipped and 23 subtests; dashboard tests/lint/format/build passed.
- Real retained source: claimed handoff `035` plus
  `autopilot/escalations/035.md` with reason `verification_incomplete`.
- Pre-change snapshot exposed only an escalation boolean/reason and one idle banner. It omitted canonical
  effective state, parked condition, metadata/action, history/detail, source health, and live delivery.

## Test-first proof retained in this change

- Canonical event contract first failed with `ModuleNotFoundError`; replay/producer suite then reached 106
  passing tests.
- Snapshot/detail tests first failed because `items` and `operational_detail` did not exist; the combined
  state/dashboard/run-history suite reached 108 passing tests.
- Stream tests first failed because `_SnapshotBroker` did not exist; sequence/replay/gap/timing tests pass.
- Render tests first failed on missing states, health dimensions, and EventSource; render/stream/dashboard
  suite reached 57 passing tests.
- Gateway tests first failed because `llm_gateway` did not exist; both adapters plus gateway contract reached
  40 passing tests.
- The first broad collab sweep exposed an invalid shipped assurance catalog with an overlapping OpenAI
  provider. The gate failed closed. The catalog now uses gateway-routed Google+Anthropic baseline and
  OpenAI+xAI high-risk profiles; the nine formerly failing representative paths pass.

## Authoritative verification

`uv run --locked python scripts/verify.py` passed after the implementation:

- 190 core tests passed; Ruff and Pyright passed with zero findings.
- 723 collab tests passed, 6 skipped, and 23 subtests passed.
- Dashboard Vitest (3 tests), lint, format, and production build passed.
- The verifier's standing scope note remains unchanged: `collab/` is outside the configured Pyright roots
  (372 pre-existing findings), and Playwright is a separate gate.

The separate `pnpm --dir dashboard e2e` suite passed its existing dashboard smoke (1 passed) and skipped
the operational test because that test deliberately requires an explicit Python dashboard URL and fixture.
The operational test was also run against the real Python dashboard and a retained collab fixture: 1 passed
in 6.4 seconds. It proved the queued → escalated → queued stream, structured reason/action, detail history,
source evidence, reload recovery, and forced reconnect recovery. The captured runtime is retained at
[`dashboard-operational-state-runtime.png`](dashboard-operational-state-runtime.png).

## Gateway and correlated Langfuse proof

- LiteLLM `/v1/models` accepted the configured virtual key and returned HTTP 200 with 10 gateway aliases.
- One minimal real request used alias `gemini-3.5-flash`, feature `dashboard-operational-proof`, request id
  `60f8dce5-578c-43be-8bff-67a8949d6565`, and trace id `runtime-browser-proof`.
- The gateway response reported 5 input tokens, 88 output tokens, and 93 total tokens.
- Langfuse observation `time-22-07-42-270512_ru1fapnfHrSmmtkPi5nv0Ak` matched that trace, feature, model
  (`gemini/gemini-3.5-flash`), and the same 5/88/93 usage tuple.
- The local attempt and remote verification records are append-only and contain correlation/status/usage
  metadata only; they do not retain prompts, completions, authorization headers, or key material.

There is no remaining external blocker for the requested gateway or Langfuse proof.
