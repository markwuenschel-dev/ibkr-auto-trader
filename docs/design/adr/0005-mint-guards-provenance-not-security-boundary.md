# ADR 0005 — Mint guards are provenance / accidental-bypass protection, not an in-process security boundary

- **Status:** Accepted — 2026-07-18 (integrity-audit flywheel, INT-005). Ratifies a threat model already
  in force; no production behaviour changes.
- **Slice:** the `_MintGuarded` domain seam (`src/ibkr_trader/domain/models.py`) and the mint authorities
  (`RISK_AUTHORITY`, `EXECUTION_AUTHORITY`, `ASSEMBLER_AUTHORITY`).
- **Companion / precedent:** `docs/plans/local-verification-and-provenance-contract.md` (**Status: Accepted**,
  2026-07-14) §10–11; `src/ibkr_trader/__init__.py:10-11`; `src/ibkr_trader/domain/__init__.py:3-5`;
  `src/ibkr_trader/execution/__init__.py`; `paper-trading-roadmap.md`; `CONTEXT.md` (Safety Invariants).

## Context

INT-005 was a `needs-human-decision` audit card: `_MintGuarded` records provenance, but `model_construct()`
bypasses `__init__` and the mint authorities are importable, so it is **not** a boundary against arbitrary
same-process code — and a passing test
(`tests/test_domain_models.py::test_model_construct_bypasses_provenance_guard_release_gate_hole`) names this
a "release-gate hole."

The card kept re-surfacing not because the answer is unknown but because it was never consolidated into one
authoritative artifact. The answer already exists, and the sources agree:

- The accepted provenance-contract plan: *"the current process contains trusted execution-core code only.
  Mint guards are therefore provenance and accidental-bypass controls, not an in-process security boundary,"*
  and *"`no-direct-strategy-orders` remains a named, unsatisfied LIVE release gate"* (§10–11).
- `ibkr_trader/__init__.py:10-11` and `domain/__init__.py:3-5` already state the same in the code's own
  docstrings.

This ADR is the missing closeout: it ratifies the threat model so the item stops being re-litigated. The
real defect INT-005 exposed is a **process** one — a blocked card with no single source of truth — not an
undiscovered security hole.

## Decision

**The mint seams are provenance / accidental-bypass protection. They are not, and are not claimed to be, an
in-process security boundary against hostile code in the same interpreter.**

| Decision | Content |
|---|---|
| **Threat model now** | The process contains **trusted execution-core code only**. Mint seams catch ordinary misuse and accidental construction (a strategy trying to build an `ExecutableOrder`, a caller minting without the designated authority) — not hostile in-process code. |
| **Known holes (accepted)** | `model_construct()` bypasses `__init__`; public `RISK_AUTHORITY` / `EXECUTION_AUTHORITY` are importable; `ASSEMBLER_AUTHORITY` is module-private **by convention, not by capability**. These are accepted under the trusted-code-only model. |
| **Honest test** | Keep `test_model_construct_bypasses_provenance_guard_release_gate_hole` as the permanent, intentionally-named record of the accepted hole. Do **not** "fix" it by closing the bypass under false pretenses. |
| **Pre-live gate (deferred)** | Before LIVE — and before any less-trusted in-process plugin seam — issuer containment (un-export the authorities, a `model_construct` policy, whatever process isolation the release gate requires) must land. That is a **separate candidate when LIVE approaches**, tracked as the `no-direct-strategy-orders` release gate; it is **not** INT-005. |
| **Non-goals** | No capability tokens, no process isolation, no public-API churn in this ADR. |

## Consequences

- INT-005 closes as **accepted / shipped as a design decision**, not "fixed in code." Its enforceable check
  is this ADR plus the hole test remaining green and intentionally named.
- The code already matches the decision, so there is no behaviour change; the pointers below make the ADR the
  single place future audits consult instead of re-deriving the answer.
- The genuine hardening work is not lost — it is named as the LIVE release gate and will be its own candidate
  when live trading (or a less-trusted plugin) is on the table.
