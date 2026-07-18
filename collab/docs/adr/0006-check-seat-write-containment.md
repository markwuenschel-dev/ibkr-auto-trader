# ADR 0006 — Check-seat write-containment is not enforced yet; isolation is the required next control

- **Status:** Accepted — 2026-07-18 (integrity-audit flywheel, INT-037). Honesty fix shipped now;
  isolation named as required follow-up (INT-037b), not a permanent posture.
- **Scope:** the `--run-checks` (read_test) seat in `collab/tools/adapters/openai-repo-seat.py` used by
  the reviewer / breaker / verifier roles (ADR-0004).
- **Related:** collab ADR-0004 (four-role risk-tiered assurance — defines read_test); `collab/SEATS.md`;
  trading `docs/design/adr/0005-mint-guards-provenance-not-security-boundary.md` (the less-trusted-seam
  carve-out this seat is an instance of).

## Context

The `--run-checks` seat is documented as unable to write the source it judges: the code comment said it
"can never write source it is judging", the system prompt said "you MUST NOT attempt to modify the
source", and ADR-0004 / SEATS.md describe read_test as "without source writes."

That guarantee is not enforced. The seat is granted `run_command` with an allow-list checked only on
`argv[0]`: `_RUN_ALLOW = {pytest, ruff, python, python3, py, uv}` (`openai-repo-seat.py:185,197`), with
`shell=False`. Allow-listing `python`/`uv` admits arbitrary code — `python -c "open('x','w')..."`, a
`uv run` script, or a test fixture — so the seat **can** mutate the tree and reach the network.

Crucially this is **not** INT-005 / ADR-0005 again. ADR-0005 accepted provenance-only guards inside a
*trusted execution-core* process. The check seat is driven by a **remote / less-trusted model** — exactly
the "future less-trusted in-process plugin seam [that] requires a separate trust-boundary design" ADR-0005
carved out. That seam is live today on breaker/verifier, not hypothetical.

## Decision

**Ship the honesty fix now; bind isolation as the required next control (not "someday before LIVE").**

| Decision | Content |
|---|---|
| **Enforced today** | No `write_file` tool on `--run-checks`; `argv[0]` allow-list `{pytest,ruff,python,python3,py,uv}`; `shell=False`; a byte cap and timeout on output. |
| **NOT enforced today** | Filesystem write-containment, network egress, and "the judge cannot mutate the judged source." `python -c`, `uv run`, and test fixtures can write. |
| **Threat model** | Check seats are **less-trusted remote agents**. ADR-0005's trusted-code-only mint model does **not** cover this seam. |
| **Interim posture (accepted)** | Tool-surface omission + prose instruction only, with the overclaims removed from code/SEATS.md so nothing states containment that isn't there. |
| **Required follow-up (INT-037b, active — not deferred to LIVE)** | Run `--run-checks` against an **ephemeral isolated root** (a working-tree copy, so uncommitted builder edits the judge must see are preserved) as `cwd`/root, leaving the judged tree untouched. Enforceable test: a deliberate write from a check seat leaves the judged tree unchanged. |
| **Non-goals of this ADR** | No isolation implementation here; no stripping `python`/`uv` from the allow-list (that breaks legitimate `python -m pytest` / `uv run`). |

## Consequences

- INT-037 closes as **accepted design / honesty shipped**: the code comment, system prompt, `--run-checks`
  help, SEATS.md, and (via a pointer) ADR-0004 no longer claim enforced write-containment.
- **INT-037b** is opened as active collab hardening — the ephemeral-isolated-root control — **not** buried
  under the pre-LIVE mint gate, because the untrusted-model exposure exists now.
- Design note for INT-037b: a bare `git worktree` of `HEAD` would miss uncommitted builder edits the judge
  must review; the primitive is an ephemeral working-tree **copy** (or equivalent) with cleanup, a perf
  budget, and a red/green containment test — a deliberate slice, not an unscoped patch.
