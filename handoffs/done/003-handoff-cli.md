---
to: reviewer
from: builder
id: 003-handoff-cli
title: "Handoff CLI + collabs.json registry (collab-kit slice 3)"
priority: high
date: 2026-07-04
status: done
approved: 2026-07-04 (independent reviewer; production-grade adversarial verification passed; all confirmed findings fixed with regressions)
guardrails: [concurrency, path-safety, data-integrity]
---

## Summary

Slice 3 is the **self-hosting handover**: a command-line entry (`handoff <collab> …`) that drives the
approved `handoff_core` state machine, plus a `collabs.json` registry mapping collab names → roots for
cross-collab `status`. After this slice the meta-collab stops moving handoff files by hand (as we did to
close out 002) and runs the CLI, which also **self-logs telemetry** on every state change (§8). Scope is
CLI **and** registry (build-order slices 3+4 combined).

## Constraints

Carries forward (must remain true): [C4] [C5] [C6] [C9] [C10] [C11] [C12] [C13] from handoffs 001/002.
New to slice 3:

- [C14] the CLI and shims are **thin** over `handoff_core`/`registry` — no reimplemented state or id logic (cf. [C8]/[C10])
- [C15] every state-changing command (`create`/`claim`/`done`/`archive`) emits a telemetry trace event (§8 self-logging)
- [C16] stable exit codes (`0` ok / `1` bad-args / `3` conflict / `4` not-found) mapped from **typed** `handoff_core` errors, never string parsing
- [C17] shims resolve `KIT_ROOT` via the `COLLAB_KIT_ROOT` install-time override, never runtime symlink resolution (DEFECT-B)
- [C18] `collabs.json` writes are atomic (tmp + `os.replace`) under `collab_lock` — concurrent `register` cannot corrupt the registry
- [C19] `create` delegates frontmatter-injection defense to `render_handoff`'s guard; the CLI never bypasses it. **The `body`/`--body` is Summary prose ONLY** — not arbitrary Markdown: a body may not open a `#`/`##` heading or a `- [ID]` bullet, so it cannot forge a `## Constraints` section and poison identity-addressed `handoff_loss` (§7.4). Document structure (constraints, sections) comes only through typed fields.

## Details

**Command surface** (`handoff_cli.py`, argparse mirroring `gate_runner._build_parser`/`main`):
`create --to --title [--from --priority --body]` · `list [--state]` · `claim`/`show`/`done`/`archive <id>` ·
`state <id>` · `orphans` · `register <name> --root <path>` · `status` (cross-collab) · `new <name>` (minimal
scaffold+register). The collab is a registry name or a path; `collab-handoff` binds it via `HANDOFF_ROOT`.

**Typed errors** — add `HandoffNotFound`/`HandoffConflict` (subclasses of `CollabError`) to `handoff_core`
so the CLI maps `4`/`3` without parsing messages ([C16]). Only edit to approved code; existing `CollabError`
catches are unaffected.

**Registry** (`registry.py`) — `collabs.json` = `{version, collabs:{name:{root,repo,reviewer,created,guardrails}}}`;
read-modify-write under `collab_lock($COLLAB_HOME/logs/.registrylock)` committed via `safe_write` ([C18]).
`status` counts each collab's handoffs by state via `handoff_core.list_handoffs`.

**Shims** — `tools/handoff` (bash, sources `collab_common.sh`), `tools/handoff.ps1` (`COLLAB_KIT_ROOT` → walk-up),
`tools/collab-handoff` (binds `HANDOFF_ROOT`), mirroring `tools/gate` verbatim.

## Resolved questions

- `handoff new` is a **minimal** scaffold (`ensure_layout` + `register`); full `newproject` (clone + template
  rendering) stays slice 8. **Resolved: accepted for this slice.**
- Telemetry log: `<collab_root>/logs/events.jsonl`, `run_id = <collab name>`. **Resolved: kept.**
- Exit codes follow §A5 (bad-args = `1`); `gate` uses `2`. **Resolved: intentional per §A5, documented.**

## Verification (production grade)

Verified in **two adversarial rounds**; every confirmed finding fixed with a regression test.

**Round 1 — five adversarial lanes** (registry-race, exit-code, shim-resolution, telemetry, injection):

| Finding | Severity | Fix (+ regression test) |
|---|---|---|
| telemetry emit failure crashed a *committed* command | HIGH | `_emit_safe` log-and-continue; `test_telemetry_failure_does_not_fail_committed_command` |
| corrupt `collabs.json` → silent full data-loss; `PermissionError` reader crash | HIGH/MED | `load()` refuse-on-corrupt + bounded retry; `test_corrupt_registry_refuses_not_clobbers`, `test_load_retries_transient_permission_error` |
| slugify `ValueError` escaped `main()`; `-h`→exit 1 | BUG | `except ValueError`→1, SystemExit code-aware (help→0); `test_unsluggable_title_is_clean_exit1_not_crash`, `test_help_exits_zero` |
| `--body` forged a `## Constraints` section → poisoned `handoff_loss`; rejected injection leaked an orphan id | MED | `_reject_unsafe_body` + validate-before-reservation; `test_body_injection_rejected`, `test_rejected_injection_does_not_leak_orphan_id` |
| registry atomicity ([C18]), exit-code surface, shim quoting | — | verified SAFE (multiprocess `register` race; 28-case exit-code table; spaced-path shim) |

**Round 2 — two reviewer blockers:**

| Blocker | Fix (+ regression test) |
|---|---|
| bash shim failed as a `~/bin` symlink (DEFECT-B recurrence) | self-locate before sourcing; `COLLAB_KIT_ROOT` install route; `test_shims.py` (verified under Git Bash) |
| stdlib `trace` shadowing silently dropped telemetry ([C15]) | by-path module load in `handoff_events`/`handoff_cli`; `test_telemetry_survives_stdlib_trace_shadowing` |
| non-blocking: distinct `handoff.archive` stage; "body = Summary text only" doc | both addressed |

**Coverage:** concurrency (registry RMW race), data-integrity (atomic writes, refuse-on-corrupt), path-safety
(frontmatter/body injection, symlink shim), exit-code contract, telemetry robustness + stdlib-shadowing,
end-to-end shim. **Retention gate `handoff_loss(002→003) = 0.0`; contract `validate_handoff(003) = []`.**

## Closeout

Approved for production-grade self-hosting handoff use.

Verification included two adversarial rounds covering registry races, exit-code contract, shim resolution, telemetry integrity, injection/body-forgery defenses, stdlib trace shadowing, and end-to-end shim behavior. All confirmed findings were fixed with regression coverage. `handoff_loss(002→003) = 0.0`; `validate_handoff(003) = []`.

Moved from `pending/` to `done/`.
