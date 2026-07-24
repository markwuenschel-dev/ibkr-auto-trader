# Four-model gateway and telemetry proof — 2026-07-22

## Verdict

All four required aliases were attempted through the canonical LiteLLM route under one true run UID, and all four fresh requests were reconciled to distinct Langfuse generation observations. GPT, Gemini, and Haiku completed successfully. Grok reached LiteLLM and xAI but xAI returned HTTP 403 because the provider team had exhausted credits or reached its monthly spending limit.

The proof therefore establishes four-model routing and telemetry coverage, but **does not establish a fresh successful four-model run**. That remaining provider-success criterion is externally blocked until xAI credits or the monthly limit are restored. The earlier retained run independently proves successful Grok execution and export.

No prompt, completion, API key, provider credential, raw request ID, raw observation ID, or chain-of-thought appears in this artifact.

## Fixed proof scope

- Run UID: `obs001-four-model-20260722T0107Z`
- Required aliases: `gpt-5.6-luna`, `grok-4.5`, `gemini-3.5-flash`, `haiku-4.5`
- Request: one minimal exact-`OK` request per alias
- Correlation: app `request_id` metadata, LiteLLM session ID, and Langfuse requester metadata
- Langfuse read: Observations API v2, bounded timestamps, fields excluding model inputs and outputs

## Reconciliation

| Alias | Gateway/provider outcome | LiteLLM spend row | Langfuse generation | Truthful disposition |
|---|---:|---:|---:|---|
| GPT-5.6 Luna | completed | yes | verified | successful |
| Grok 4.5 | xAI HTTP 403 | no | verified | provider dependency unavailable |
| Gemini 3.5 Flash | completed | yes | verified | successful |
| Haiku 4.5 | completed | yes | verified | successful |

For the four fresh attempts, the app retained four attempts, LiteLLM retained three successful spend rows, and Langfuse retained four generation observations. These counts differ for a known reason: LiteLLM did not create a successful spend row for the xAI rejection, while the Langfuse failure callback did export the failed generation.

An earlier Haiku request made before adding the alias to the app virtual-key allowlist is outside the four fresh attempts. It remains explicitly recorded as HTTP 403 with telemetry missing after the export grace period; it is not folded into gateway health.

## Provider routing evidence

Read-only LiteLLM aggregation for the successful rows resolved:

- `gpt-5.6-luna` → `openai/gpt-5.6-luna` → OpenAI;
- `gemini-3.5-flash` → `gemini/gemini-3.5-flash` → Gemini;
- `haiku-4.5` → `anthropic/claude-haiku-4-5-20251001` → Anthropic.

The Grok error body identifies the xAI team quota/credit boundary after LiteLLM accepted the alias. The redacted machine receipt stores only SHA-256 prefixes for request and observation IDs.

## Prior successful Grok evidence

The fixed window for archived run `20260721T234615Z-140256` contains seven app-recorded successful Grok requests, all seven matched to Langfuse, within twelve successful xAI spend rows. That proves the alias and export path have completed successfully in this environment; it does not override the fresh provider-limit failure.

## Verification state

- ibkr authoritative Python verifier: 190 core tests, Ruff, Pyright, and 742 collab tests passed; six skipped; dashboard/browser scopes intentionally omitted by `--python-only`.
- gateway repository: 173 tests passed, sixteen skipped; Ruff passed; strict mypy passed.
- seat policy validation: the local runtime Haiku seat now uses the canonical gateway adapter; direct-provider bypass validation passes.

Machine-readable receipt: `dashboard-human-observability-four-model-2026-07-22.json`.
