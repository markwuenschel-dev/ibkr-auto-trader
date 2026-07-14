# Configuring the four collab seats

Copy [seats.example.json](./seats.example.json) to your local seats.json, set only local paths and credentials, then restart the driver. The config has exactly four logical roles:

| Seat | Access | What it may do |
|---|---|---|
| builder | write | edit source and run allow-listed checks |
| reviewer | read_test | inspect the whole repo, run bounded checks, decide provisional sign-off |
| breaker | read_test | attack the candidate without source writes |
| verifier | read_test | reproduce/refute breaker findings without source writes |

read_test means full repository visibility at the configured repo root plus allow-listed test/lint commands; it does not mean a text-only prompt or a source-write grant. Every assessor must use a repo-capable adapter. openai-compatible-seat.py is text-only and is deliberately invalid for reviewer, breaker, and verifier. The OpenAI repo adapter uses --run-checks for read_test, never --write.

## Default profiles

The dashboard still has only the four cards above. A profile selects the existing breaker and verifier for a verification pass; it is evidence metadata, not an additional agent.

| Pass | Breaker | Verifier | When |
|---|---|---|---|
| baseline | opus-4.8 | sonnet-5 | every candidate |
| high-risk-diverse | gpt-5.6-luna | grok-4.5 | any money, execution, auth, broker, integration, market-data, time, data-integrity, or concurrency guardrail |

The high-risk providers must be disjoint from the baseline providers, and the breaker and verifier must have different execution fingerprints. An invalid or unavailable high-risk profile blocks assessment; it never silently downgrades to baseline-only evidence.

The example's builder is gpt-5.6-terra (write) and reviewer is grok-4.5 (read_test). Baseline breaker/verifier models inherit from their canonical seats, so a dashboard model switch is meaningful and is validated before it is saved. The high-risk profile pins its own diverse overrides.

## Assurance behavior

The resolver creates one immutable plan per candidate:

- Baseline always includes change regression and adds matching generic controls for untrusted output, bounded autonomy, path safety, process isolation, and concurrent-data integrity.
- A high-risk candidate gets one additional composite pair, regardless of how many safety-critical guardrails match. It combines matching order/idempotency, market-time, broker-reconciliation, and state/retry contracts.
- A breaker returns at most three findings in the form FINDING: F<n> | path | trigger | impact. One verifier call must return exactly one verdict per finding id. Malformed or missing output is verification_incomplete.

The candidate id includes the resolved plan, prompt revision, reviewer profile, selected profiles, and policy fingerprints. The immutable ledger records the plan digest and actual reviewer seat. The dashboard lane panel shows pass/profile badges without creating any extra role card.

## Budget and safety

Balanced limits permit three work attempts, six verification passes, eighteen total model calls, and three findings per pass. A normal run tops out at twelve calls; a high-risk run tops out at eighteen. Reviewer assessment still runs concurrently with the selected assurance pair or pairs, but nothing can reach done until the reviewer, every resolved pass, source/test evidence, and the done contract agree.

For the full decision record, see [ADR-0004](docs/adr/0004-four-role-risk-tiered-assurance.md).
