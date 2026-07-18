# 4. Four-role, risk-tiered assurance lanes

Status: accepted (2026-07-14). Amends ADR-0002 and ADR-0003. ADR-0003's reviewer-versus-lanes concurrency and exclusive lease lifecycle remain unchanged.

## Context

The old lane configuration fanned one risk class into string labels. It neither bound the selected assurance policy to a candidate nor distinguished an extra verification pass from an extra visible agent. It also sent a verifier one call per finding. Separately, a read-only reviewer or text-only adapter could not inspect the full repository and run the evidence checks required for a safe sign-off.

The system needs stronger scrutiny for trading-critical changes without turning every guardrail into an unbounded cost or a new dashboard role.

## Decision

### D1 — Exactly four logical seats

| Seat | Managed access | Authority |
|---|---|---|
| builder | write | make scoped source changes and run allow-listed checks |
| reviewer | read_test | inspect the full repo, run bounded checks, make a provisional sign-off decision |
| breaker | read_test | attack the candidate without source writes |
| verifier | read_test | reproduce or refute breaker findings without source writes |

read_test means a repository-capable adapter receives the configured repo root and may inspect the full tree plus run bounded checks. It grants no `write_file` tool. **Qualified by [ADR-0006](0006-check-seat-write-containment.md):** "no source writes" is a tool-surface grant, not enforced containment — the allow-listed interpreters can still write; an ephemeral isolated root is the required follow-up (INT-037b). The text-only OpenAI-compatible adapter is invalid for every assessment seat. This supersedes ADR-0003 D3's reviewer read row.

The four names remain the dashboard's only role cards. A pass or profile is ledger evidence, never a fifth agent.

### D2 — Resolve one frozen, two-pass plan per candidate

verification_plan.py converts telemetry/lanes.json version 2, seats.json, and normalized guardrails into immutable LaneSpec, LanePass, and VerificationPlan records. The same resolved object feeds both candidate-id generation and lane execution. Its canonical digest, selected contracts, profile/model/policy fingerprints, artifacts, and actual reviewer seat are written to the immutable candidate ledger.

Every candidate receives one baseline breaker-to-verifier pair. It always contains change-regression, plus the matching generic controls for untrusted output, bounded autonomy, path/pointer safety, process isolation, and concurrent-data integrity.

Any of money, execution, auth, broker, integration, market-data, time, data-integrity, or concurrency adds exactly one high-risk-diverse composite pair. Multiple matches extend that one pair's checklist; they never create more pairs. Its focused contracts cover order risk/idempotency, market-data causality/time, broker snapshot/reconciliation, and state concurrency/retry, with matching boundary controls for auth and integration.

The baseline profile is opus-4.8 breaker plus sonnet-5 verifier. The high-risk profile is gpt-5.6-luna breaker plus grok-4.5 verifier. Provider metadata is mandatory; high-risk providers must be disjoint from baseline providers, and breaker/verifier execution fingerprints must differ within each pair. Invalid, unavailable, text-only, write-capable, or insufficiently diverse profiles fail before assessment and never fall back to baseline.

Version-1 lane fan-out configuration is obsolete and rejected by the v2 resolver.

### D3 — Bounded, batch verification

One breaker call may return at most three identified findings: FINDING: F1 | path | trigger | impact. One verifier call receives the full batch and returns exactly one verdict per finding id: CONFIRMED or REFUTED.

Missing, duplicate, unsupported, malformed, or prose-contaminated batch output is verification_incomplete; it is never clean. Confirmed verdicts require evidence. A finding cap or budget denial is also incomplete; backend or adapter failures remain infrastructure_blocked.

### D4 — Accounting and closure bind to passes

The budget charges one verification pass per breaker-to-verifier pair and one verification call per model dispatch. Balanced limits are three work attempts, three findings per pass, six verification passes, and eighteen total model calls. Across three attempts, a normal baseline run tops out at twelve model calls and a two-pass high-risk run at eighteen.

The done contract reads required pass ids from the immutable plan in the ledger, not mutable current config. Every required pass must have run and the ledger must be neither incomplete nor a tool error. Reviewer and lanes still run concurrently under ADR-0003; the aggregate decides only after both complete.

### D5 — Dashboard evidence, not extra cards

The dashboard continues to show only builder, reviewer, breaker, and verifier. Its lane panel labels existing entries with pass, profile, composite, and contract metadata. Before saving a managed model switch, it recompiles all canonical policies and resolves both profiles. A switch that weakens repository capability, changes an access policy, or collapses provider diversity is refused before seats.json is written.

## Consequences

- Copy and adapt seats.example.json: every canonical seat needs managed role and access, and every assessment seat needs a repository-capable adapter.
- Profile or policy changes mint a new candidate id, so evidence cannot be reused under a different lens.
- Historic direct lane calls remain only for read compatibility; they are not a way to configure v2 autonomous assurance.
- The visible team stays small while high-risk work gets a second independent check.
