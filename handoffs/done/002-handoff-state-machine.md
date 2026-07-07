---
to: reviewer
from: builder
id: 002-handoff-state-machine
title: "Handoff state machine + id allocation (collab-kit slice 2)"
priority: high
date: 2026-07-04
status: done
approved: 2026-07-04 (independent reviewer; 13 pass, crash-residual blocker resolved)
guardrails: [concurrency, path-safety, data-integrity]
---

## Summary

Slice 2 builds the handoff state machine on top of the slice-1 substrate (`collab_common`): id
allocation and the `pending → claimed → done → archive` lifecycle, plus `list`/`show`. It is the
first real *consumer* of `collab_lock` + `assert_current` + `exclusive_create`, so it must honor the
composition constraints slice 1 discovered — carried forward here by ID.

## Constraints

Carries forward from handoff 001 (must remain true): [C1] [C2] [C3] [C4] [C5] [C6] [C7] [C8].
New to slice 2:

- [C9] id-uniqueness is committed via `exclusive_create` on an **id-only** ledger path (`.ids/{NNN}.id`), never the slugged filename — the realization of [C4]
- [C10] state transitions are a single-winner atomic `os.link` CAS + `unlink` (**`os.replace` is NOT single-winner on Windows** — `MoveFileExW` lets N racers all succeed; a no-overwrite hard link is a true compare-and-swap); the directory is the **sole** source of truth, and `status` frontmatter is creation-time metadata only (never updated on transition — derive state via `state_of`)
- [C11] `handoff_loss(001 → this)` must be 0.0 for the carried-forward slice-1 constraints (self-checking retention)
- [C12] every created handoff validates against the typed contract (`contracts.validate_handoff` → [])
- [C13] concurrent `create` from N processes yields N unique, non-colliding ids (adversarially tested, per slice-1 rigor)

## Details

**Layout.** `<collab>/handoffs/{pending,claimed,done,archive}/{NNN}-{slug}.md` for content; a
permanent id ledger at `<collab>/handoffs/.ids/{NNN}.id` is the single source of id allocation.

**`create`** (the constraint-critical path):
1. Under `collab_lock(.idlock)` (short section, generous ttl — [C6]): `nid = 1 + max(id across the `.ids` ledger AND the `{NNN}-*.md` prefixes in all four state dirs)` — scanning both closes the manual→CLI migration collision (lane 1); the ledger `exclusive_create` stays the id-uniqueness backstop.
2. `assert_current()` fence, then `exclusive_create(.ids/{NNN}.id)` — the id-only atomic commit ([C4]/[C9]). `FileExistsError` ⇒ a *pre-commit* collision ⇒ recompute + retry (retry-safe per [C5]).
3. Write `pending/{NNN}-{slug}.md` (also `exclusive_create`).
4. A `LockBroken` raised **after** the reservation commit is treated as success — never retried ([C5]).

**`claim`/`done`/`archive`**: single-winner `os.link(src, dst)` CAS then `os.unlink(src)` ([C10]); `FileExistsError`/`ENOENT` ⇒ lost the race, reported, not retried. (Adversarial verification proved `os.replace` is **not** single-winner on Windows — N racers all "succeed" — so the hard-link CAS is required.)

**`list`/`show`**: read-only scans of the state dirs.

## Risks & Questions for Reviewer

- ~~Is the `.ids/{NNN}.id` ledger the right id-uniqueness mechanism vs. scanning all four state dirs?~~ **Resolved — use both:** the ledger `exclusive_create` is the id-uniqueness/commit backstop; scanning all four state dirs raises the allocation floor so manual/legacy handoffs can't cause reuse (lane 1).
- Should `claim`/`done` rewrite the `status` frontmatter field (second write, breaks the single-atomic-rename property) or leave the directory as sole truth? Proposed: leave it; directory is truth.
- Registry (`collabs.json` + `register`/`status`) is deferred to slice 2b unless you want it in this pass.

## Request

Review the id-allocation design against [C4]/[C5]/[C6] specifically — the reservation-then-content
two-commit sequence and the post-commit `LockBroken` handling. On sign-off, the implementation ships
with an adversarial concurrent-create test ([C13]) and a `handoff_loss(001 → 002)` == 0 check ([C11]).
