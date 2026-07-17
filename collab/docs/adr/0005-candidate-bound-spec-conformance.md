# 5. Candidate-bound spec conformance; v2 assurance is mandatory

Status: accepted (2026-07-16). Amends ADR-0004 D2 (the two-pass rule) and D4 (the budget). ADR-0004's four logical roles, `read_test` definition, profile diversity rules, and bounded batch protocol remain unchanged. No fifth seat and no new dashboard card.

## Context

Two failures on 2026-07-16, both observed on a real run of handoff 035.

**The machinery was inert.** ADR-0004's resolver (`verification_plan.py`) was fully built and tested, and `_resolve_assurance_plan` returned `None` for any catalog without `assessment_profiles` — so every real run silently took the legacy generic fan-out. Nothing validated seats, so a text-only adapter occupied the verifier seat: a seat that can read the handoff TEXT but not the code was adjudicating code. Candidates were assessed with no resolved plan bound into their ledger. "Unmigrated" was indistinguishable from "configured"; silence read as safety.

**The gate could not see an omission.** The builder implemented PT-6; the reviewer signed off; all three adversarial lanes reported zero confirmed defects; the authoritative gate was green. An audit then found `RiskPlan.decision_generation` and `.session_generation` were structurally always `None` — `planner.py` read attributes that do not exist on `RiskContext` or `RiskControlState`, while ADR-0003 requires plans to bind them.

Nothing caught it, and each miss was structural rather than a lapse:

- **pyright** was blind: the field is typed `Any = None`, so `None` is valid.
- **The tests** were the builder's own. The blind spot that omits a binding also omits its test.
- **The lanes** adjudicate *raised* findings. The ten contracts in `lanes.json` are defect probes — they ask "what is WRONG here?". An omission is not wrong; it is absent, and absence raises no finding.
- **The reviewer** marked it `[met]`, citing a real, resolvable line range and a real test that asserts different fields. It mistook a written assignment for a live binding. Its itemization is advisory by design (`narrative.py`), so nothing checked the claim.

The reviewer's semantic judgement reaches the gate as exactly one bit: token present + seat authorized ⇒ `APPROVED`. Everything richer was display-only. No condition gated semantic coverage of requirements.

## Decision

### D1 — Autonomous closeout requires a valid v2 catalog. There is no legacy fallback.

`_resolve_assurance_plan` raises on a v1, missing, or profile-less catalog, before any builder dispatch; the caller escalates `infrastructure_blocked` naming the migration. `lanes.run_lanes` remains available for direct/manual use — it simply cannot reach autonomous done.

Done-contract condition 3 (`lanes-ran`) additionally requires the resolved plan to be **present and bound** in the ledger. Without a plan, required passes fall back to *mutable current config*, and for a candidate with no guardrails that is **zero** required passes — the condition passing vacuously.

### D2 — Every candidate runs one always-on, paired spec-conformance assessment.

Not an eleventh `LaneSpec`: those batch defect probes, and one protocol cannot mean two things. A dedicated `ConformancePass` reuses the **baseline** profile (always resolved, so no new seat) and speaks its own strict per-requirement JSON protocol. This amends ADR-0004 D2's "never more than two passes".

Requirements are the handoff's **declared, typed constraints** — the existing `## Constraints` / `- [ID] text` scheme (§7.2), already injection-defended. Conformance does not parse handoffs itself: a second parser would be a second truth. **A handoff declaring no typed constraints cannot close autonomously**, and is refused before any model work: an autonomous close asserts the spec was met, and with no requirements that assertion is unfalsifiable.

The compiled contract is digested and bound into candidate identity, so changed constraints or a changed protocol cannot reuse prior evidence.

`satisfied` requires: both assessors parse strictly; both cover **exactly** the declared ids; both **agree** per id; every id is `met`; and every cited source pointer **resolves** inside the repo. Anything else — disagreement, malformed output, a stale digest, an unresolvable pointer, a dead backend, an exhausted budget — is `incomplete`. Disagreement is not a tie broken toward shipping: if two assessors disagree about whether a requirement is met, the honest state is *unknown*, and unknown must never close. A verifier-confirmed `missing`/`partial` becomes a blocking finding carrying the requirement text, so the builder receives the exact unmet item.

Reviewer `[met]` prose stays advisory. It is **not replaced**, because it was never authoritative — prose that grades itself is what failed.

### D3 — Done-contract condition 12 gates conformance, sourced from the ledger.

Like every other condition, and for the same reason as condition 11: enforced in the contract so no other `hc.done` caller can bypass it. Fail-closed on absence, evidence bound to another candidate, incompleteness, any unmet id, coverage mismatch, and an empty contract.

### D4 — Balanced budget 3/6/18 → 3/9/24.

Three passes per attempt over three attempts, and two model calls for the conformance pair. Left at 6/18 the third pair would exhaust the budget mid-candidate and every run would escalate `budget_exhausted` instead of closing. This amends ADR-0004 D4.

## Consequences

**Operators must migrate `seats.json` to v2** or the driver refuses to dispatch. Intended: that refusal is the decision.

**Every autonomously-closable handoff must declare typed constraints.** New authoring obligation; `handoff create --constraint ID=TEXT` supports it. Historical `done/` handoffs are not rewritten.

**Two model calls on every candidate, forever.** The standing price of independent agreement.

**This is an enforced completeness check, not proof of arbitrary natural-language truth.** Stated plainly because the boundary is easy to forget and expensive to misremember: every mechanical check here concerns coverage, shape, and whether a citation *resolves* — none of them touches whether the cited line supports the claim. The reviewer's false `[met]` cited a real file at a real line; that exact record **passes every check in this design**. The only thing standing between it and a close is the second assessor independently disagreeing. This is pinned as a passing test (`test_both_assessors_agreeing_on_a_false_met_STILL_PASSES`) rather than left in prose, so it cannot quietly be forgotten.

So conformance upgrades the gate from *one model's unverified paragraph* to *two independent models must agree, with resolvable citations*. That is a real improvement and it is not proof.

**Therefore: a requirement that can be expressed as a type or a test should be one.** Deterministic types, tests, and `scripts/verify.py` remain the authority wherever a requirement is mechanically expressible — they cannot be talked out of a verdict. Conformance is for the requirements that genuinely cannot be ("the planner owns reduction; nothing downstream re-sizes"). The generation binding that motivated this ADR is itself a case in point: typing `decision_generation: int` would make that specific bug a pyright error, caught free on every run. Building this gate does not remove that obligation.
