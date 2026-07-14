# Local Verification and Provenance Contract

**Status:** Accepted
**Date:** 2026-07-14
**Mission:** Make the documented local verification command an honest, fail-closed release signal, and align mint-guard language with the accepted trusted-code-only architecture.

## Decisions bound by this contract

- Verification remains a **local** command. This change creates no CI workflow.
- The current process contains trusted execution-core code only. Mint guards are therefore provenance and accidental-bypass controls, not an in-process security boundary.
- `no-direct-strategy-orders` remains a named, unsatisfied LIVE release gate. A future less-trusted in-process plugin seam requires a separate trust-boundary design before it ships.
- The result is committed on a new branch and is not pushed.

## Scope

1. Ship Pyright as a locked development dependency and include it in the aggregate verifier.
2. Make `scripts/verify.py` fail closed when the full dashboard toolchain is unavailable; lock validation must precede all `uv run` work, and all such work must use the locked dependency graph.
3. Keep explicitly narrowed commands usable, but label them `PARTIAL PASS`; they must never claim the whole checkout is green.
4. Exclude broker integration tests unconditionally from the normal local matrix.
5. Add a focused test of verifier-plan policy, configure Oxfmt so its existing check is reproducible, and correct claims that minting/type seams make bypass impossible.

## Non-goals

- No GitHub Actions or other CI infrastructure.
- No process-isolation, capability-token, or issuer-containment implementation.
- No changes to KICKOFF/roadmap current-status policy; that requires a separate decision about their ownership and purpose.
- No cleanup of the existing collab whole-repo Ruff debt.

## Acceptance evidence

- Default verification includes lock, core tests/lint/type checking, collab tests, and every dashboard gate, or fails explicitly when a required tool is unavailable.
- `--python-only` and `--no-build` show an explicit partial result.
- The default test command cannot invoke the `integration` marker even if `IBKR_INTEGRATION` is set.
- Focused verifier tests, the full local matrix, and a diff review pass before commit.
