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

## Addendum — INT-037b implemented (2026-07-19)

The short grill that preceded the slice settled the four forks ADR-0006 left open; the decisions and
their residuals are recorded here so the next reader does not re-derive them.

| Fork | Decision | Rationale |
|---|---|---|
| **Isolation home** | The **OpenAI seat self-isolates** on `--run-checks` (not `--write`): `main()` copies `--repo-root` and rebinds `root` for the whole tool loop (`openai-repo-seat.py` `_isolate_check_root`). | `--repo-root` is already the judged tree; the process that must not write is the natural enforcement point, and it keeps the change local + unit-testable. |
| **Copy contents** | A working-tree **copy** (never `git worktree` of HEAD — uncommitted builder edits must be present), excluding `_COPY_SKIP` = `_SKIP_DIRS` (incl. `.git`, caches, vendored, `dist`/`build`) plus `.next`/`.hypothesis`/`.tox`/`target`/`coverage`. `tempfile.mkdtemp` **outside** `--repo-root`. | Reuses the seat's existing skip vocabulary so list/search/copy agree on "the repo"; `.git` excluded (containment + pytest don't need it; receipts bind the judged root via the driver). |
| **Lifecycle** | `try/finally` `rmtree` on the copy; `COLLAB_KEEP_CHECK_ROOT=1` retains it for debugging; the temp path never leaks into `--write` mode. | Deterministic cleanup, a debug escape hatch, no cross-mode leakage. |
| **Enforceable check** | Unit-level, no network (`test_openai_repo_seat.py::TestCheckRootIsolation`): the copy carries uncommitted edits + excludes `.git` + is external; a `run_command` `python -c` write lands only in the copy and leaves the judged tree unchanged; the keep-env retains the copy. | The ADR-0006 red/green containment check, runnable in CI. |

**Residuals (explicitly out of this slice):**

- **Claude `read_test` still runs allow-listed Bash on the real root** (`adapter_profiles.py` `_TEST_BASH_TOOLS`). Caller-level isolation for the Claude path is **INT-037c**, not done here.
- **A model that writes a symlink inside the copy pointing out** could still escape a *write* (reads are already contained by `_safe_path`). This copy raises the bar against ordinary/accidental writes; perfect containment remains a container (the deferred end state).
