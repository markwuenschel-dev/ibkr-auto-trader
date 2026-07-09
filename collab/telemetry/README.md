# Telemetry — the trace envelope (v0.1)

> Grounds out ARCHITECTURE.md **§8** ("log first; everything depends on traces") at the scope we
> actually have today: a builder → reviewer → adversarial-breaker → human loop with a `pytest`
> oracle. This is deliberately the *minimum* that turns the loop we're already running into data.
> Risk/calibration fields exist in the schema but are **null until those layers exist** (§11 order:
> log first, risk model second). We are honest that today we run the **verify-everything ceiling
> (B6)** — no waiver, no calibration — so `risk.waived` is always false and `decision.confidence`
> is always null: authority came from *evidence*, not a score (§5.6).

## Why this is the next step, not slice 2

Per §13, item 1 is observability and item 2 is typed contracts — both *before* more mechanism. We
already generated a rich trace (two review rounds, 11 findings, 4 confirmed HIGH bugs) and **kept
none of it**. This envelope retrofits that run (`traces/slice-01-collab-common.jsonl`) so it becomes
the seed of the calibration set every later layer (§6 risk model, §5.7 staged authority, §10
ablation) needs.

## The event envelope

One JSONL line per event, append-only, content-addressable. Every line is independently parseable.

```json
{
  "schema_version": "0.1",
  "ts": "<ISO-8601 UTC>",
  "run_id": "<stable per task>",
  "span_id": "<this event>",
  "parent_span_id": "<causal parent | null>",
  "stage": "intake|handoff.create|handoff.revise|review|build|test.run|verify.lane|finding|fix|handoff.done|handoff.archive|final.decision",
  "role": "orchestrator|builder|reviewer|breaker:<lane>|service|human",
  "artifact": "<what this event is about>",
  "artifact_version": "<rev1|impl-3|... | null>",
  "decision": { "action": "route|handoff|produce|revise|accept|reject|waive|escalate",
                "reason_codes": [], "confidence": null },
  "metrics": { "passed": 0, "total": 0, "files": 0, "latency_ms": null, "cost_usd": null },
  "gates":  [ { "name": "pytest", "status": "pass|fail", "severity": "blocking|advisory" } ],
  "eval":   { "verdict": "...", "lane": "...", "findings": 0 },
  "finding":{ ...see below... },
  "risk":   { "p_error": null, "ucb": null, "tau": null, "waived": false },
  "failure":{ "class": "...", "severity": "...", "escaped": false }
}
```

### The `finding` record — the calibration unit (§5.6 evidence ladder)

The one field worth getting right now, because it's what §6.3 (unique-catch rate), §5.7/§10.5
(reject-precision), and §11 (risk model labels) are all computed from later:

```json
"finding": {
  "finding_id": "F7",
  "title": "o-binary-newline-corruption",
  "severity": "high|medium|low|info",
  "evidence_level": "claim|located|triggered|executable",   // §5.6 ladder — authority scales with this
  "verdict": "confirmed|refuted|plausible|constraint|clean", // was the finding real?
  "converged_lanes": ["commit-atomicity","filesystem-residual"], // >1 lane => higher confidence (§6.3)
  "disposition": "fixed|mitigated|documented|accepted_residual",
  "fix": "os.open | getattr(os,'O_BINARY',0)",
  "fixed_by_test": "test_8_newline_payload_is_byte_exact"     // executable => the finding became an oracle (§5.6)
}
```

## Field → ARCHITECTURE.md mapping

| Field | Section | Status today |
|---|---|---|
| `stage` / `role` / span tree | §8 trace hierarchy | **live** |
| `finding.evidence_level` | §5.6 evidence ladder | **live** (this is the point) |
| `finding.converged_lanes` | §6.3 diversity / unique-catch | **live** |
| `gates[]` (pytest) | §5 L3 execution oracle | **live** |
| `decision.confidence` | §6.1 calibrated score | **null** — no calibrated model yet |
| `risk.{p_error,ucb,tau,waived}` | §6 waiver policy | **null** — running B6, no waiver |
| `eval.judge_mode / panel / kappa` | §5.3 judge hardening | **reserved** — no LLM judge yet |
| `handoff_loss` per edge | §7.4 | **reserved** — needs typed contracts (next) |

## Emitting

Events append through `tools/lib/trace.py`, which serializes each line and appends **under a
`collab_lock`** (from slice 1) so concurrent writers can't interleave a JSONL line — the substrate we
just built and verified is what makes the audit log crash-safe. The log is a committed file (§8:
"start on Netlify static reading a committed JSONL log"), doubling as the immutable audit trail.

## What this seed trace already lets you compute (with no ML)

From `traces/slice-01-collab-common.jsonl` alone — see `metrics.md` for the numbers:

- **Per-lane yield & unique-catch** (§6.3): which breaker lanes found real bugs vs. noise.
- **Evidence-level distribution** (§5.6): how many findings reached `executable` (became tests).
- **Confirmed vs. constraint vs. clean** — the start of a reject-precision denominator (§5.7/§10.5).
- **Rounds-to-converge & test-count trajectory** (§9): 17 → 24 → 30 → 33.
- **The honest gap**: `risk.waived` is false on every event — we paid full verification cost. That's
  the B6 baseline the eventual savings engine (§6) has to beat.
