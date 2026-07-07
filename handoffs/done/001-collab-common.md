---
to: reviewer
from: builder
id: 001-collab-common
title: "Implement collab_common (path resolution, atomic IO, race-safe mkdir-lock, slugify)"
priority: high
date: 2026-07-03
status: done
revision: 3
guardrails: [concurrency, path-safety]
approved: 2026-07-03 (independent reviewer; 32 pass + 1 win-only skip reproduced)
---

> **APPROVED — moved claimed → done.** Reviewer reproduced the suite (32 pass, 1 Windows-only
> MSYS test skipped on Linux) and cleared slice 2 to begin. Post-approval micro-hardening added:
> `_write_all` now raises `CollabError` on a zero-progress `os.write` (test `test_9`; 33 pass total).
> Binding slice-2 constraints recorded in "Verification round 2" below.

> **Revision 3 (reviewer takeover amendments).** Adds **owner-token fencing** (Amendment A) and
> precise **KIT_ROOT naming** (Amendment B), plus an exclusive-create commit defense and the final
> approval-bar invariant. The v2 release logic had a real defect: an unconditional `rmtree` on
> release would delete a *newer* owner's lock dir if ours had been stale-broken. Fencing fixes it.
>
> **Approval-bar invariant:** *At any moment, a process may perform a lock-protected final mutation
> only if it currently holds the canonical lock directory **and** `lockdir/meta.json`'s owner token
> matches its `LockHandle.owner_token`.*

## Constraints

Declared, typed constraints (identity-addressed for `handoff_loss`, §7.4). Slice 2 and every higher
slice must carry these forward by ID.

- [C1] Lock-protected final mutation only while holding the canonical lock dir AND `meta.json` owner_token matches (approval-bar invariant)
- [C2] Concurrency guardrail: no two processes may hold the same canonical lock simultaneously
- [C3] Path-safety guardrail: untrusted names are slug-sanitized before touching the filesystem
- [C4] Slice 2 must key id-uniqueness on the id alone, not the slugged filename path
- [C5] Slice 2 must never retry a `LockBroken` raised after a successful `exclusive_create` commit
- [C6] Slice 2 must keep `ttl` comfortably above the allocation/commit critical section
- [C7] stdlib only — no third-party Python packages; no heartbeat, no daemon
- [C8] `collab_common.sh` stays path-resolution only — no duplicated locking/atomic-IO logic

## Summary

Revised proposal for collab-kit slice 1 — the foundational `collab_common` core that every higher
slice depends on. This revision (v2) responds to the independent reviewer's conditional approval by
tightening the five required areas: **lock semantics, path-resolution edge cases, atomic-write
robustness on Windows, error/diagnostic surface, and tests-with-this-slice.** Where the review asked
for "document precisely," this spec pins down concrete algorithms so the reviewer can sign off on the
*design* of the concurrency primitive before any code is trusted.

## Scope & module split (clarifies reviewer minor #3)

Logic lives once, in Python. The `.sh` is intentionally thin.

| File | Owns | Must NOT contain |
|---|---|---|
| `tools/lib/collab_common.py` | path resolution, `slugify`, `atomic_write`/`safe_write`, the lock, exceptions | — |
| `tools/lib/collab_common.sh` | `KIT_DIR`/`COLLAB_HOME` resolution for the **bash shim only** | any locking or atomic-IO logic (single source of truth is the `.py`) |

Public Python API:
`resolve_kit_root()`, `resolve_collab_home()`, `collab_root(name)`, `slugify(s)`,
`atomic_write(path, data)`, `safe_write(path, data, *, retries, backoff)`,
`exclusive_create(path, data)` (O_EXCL commit helper, Amendment/commit-defense),
`collab_lock(lockdir, *, ttl, acquire_timeout) -> LockHandle` (context manager),
`is_lock_held(lockdir)` (advisory only).

`LockHandle` fields/methods (Amendment A — fencing):
`owner_token: str`, `path`, `meta_path`, `acquired_at`, `assert_current()` (raises `LockBroken`
unless `meta.json` exists and its token equals `owner_token`).

---

## Response to review — point by point

### 1. Lock semantics (Concurrency guardrail) — RESOLVED, hardened beyond the ask

The naive "detect stale → rmdir → mkdir" is itself racy (two processes both break, both acquire).
This spec replaces it with a **race-safe break via atomic rename**. Exactly one process can win the
rename, so exactly one break happens.

**`collab_lock(lockdir, *, ttl=30.0, acquire_timeout=60.0)`** algorithm:

```
acquire:
  token = f"{host}:{pid}:{time_ns}:{urandom8_hex}"     # unique per acquisition (Amendment A)
  loop (until acquire_timeout, else raise LockTimeout):
    try: os.mkdir(lockdir)                      # atomic create = the lock
      -> atomic_write(lockdir/meta.json, {owner_token: token, pid, host, acquired_at, ttl})
         (on failure: rmtree(lockdir); raise)   # never return a handle without our token committed
      -> return LockHandle(path=lockdir, owner_token=token, acquired_at=now)
    except FileExistsError:
      age = now - os.stat(lockdir).st_mtime      # mtime is the age source of truth, NOT meta.json
      if age <= ttl:                             # live lock -> wait
        sleep(backoff); continue
      else:                                      # stale -> SAFE break: rename CLEARS, it does NOT acquire
        graveyard = f"{lockdir}.broken.{now_iso}.{pid}.{urandom4_hex}"
        try: os.rename(lockdir, graveyard)       # atomic; only ONE renamer wins; loser -> FileNotFoundError
          -> log.warning("broke stale lock", old_meta=read_or_none(graveyard/meta.json))
          -> shutil.rmtree(graveyard, ignore_errors=True)
          -> continue                            # re-enter normal acquire loop; a FRESH mkdir wins ownership
        except (FileNotFoundError, PermissionError):
          continue                               # someone else broke/holds it; loop and wait

assert_current(handle):                          # call IMMEDIATELY before any final mutation
  m = read_or_none(lockdir/meta.json)
  if m is None or m.owner_token != handle.owner_token: raise LockBroken   # fail-safe on missing/partial

release (context manager __exit__) — FENCED (Amendment A):
  m = read_or_none(lockdir/meta.json)
  if m is not None and m.owner_token == handle.owner_token:
      shutil.rmtree(lockdir)                     # only remove OUR lock
  else:
      raise LockBroken                           # do NOT remove anything — it may be a newer owner's lock
```

**Explicit (reviewer correction):** the stale-break winner does **not** assume ownership. `rename`
only *clears the obstruction*; there is a gap after rename, and any contender may legitimately win
the subsequent fresh `mkdir`. Ownership is real only once a process has (a) won `mkdir(lockdir)` and
(b) committed its own `owner_token` to `meta.json`. `meta.json` is written via `atomic_write` so a
reader never observes a partial token.

**State machine (as the reviewer requested):**

| Observed state | Action |
|---|---|
| No lock dir | `mkdir` → **acquire** |
| Lock dir, `age ≤ ttl` | **wait** (poll w/ backoff) until it frees or `acquire_timeout` → `LockTimeout` |
| Lock dir, `age > ttl` | **break via atomic rename** (single winner) + `warn`, then re-compete |
| Break race lost | fall back to **wait** |

**Residual risk (documented, honest):** a *live-but-slow* holder whose op exceeds `ttl` can have its
lock broken, admitting a second holder. Mitigations: (a) `ttl` is **per-call-site configurable**
(reviewer minor #1 — accepted) so long ops set a generous ttl; (b) `release` detects the break
(`LockBroken`) so the victim fails loudly rather than silently corrupting. We do **not** attempt
cross-platform pid-liveness checks — `os.kill(pid, 0)` is not portable to native Windows in stdlib,
and faking it would be worse than the honest ttl contract. `meta.json` carries `pid` for human
diagnostics only.

### 2. Path resolution edge cases — RESOLVED (Amendment B: precise naming)

Four distinct, unambiguous names replace the overloaded `KIT_DIR`:

```
SCRIPT_PATH = resolved real path of the currently running script (symlinks followed)
SCRIPT_DIR  = SCRIPT_PATH.parent
KIT_ROOT    = the collab-kit repository root
TOOLS_DIR   = KIT_ROOT / "tools"
```

- For `tools/handoff` (shim): `SCRIPT_DIR == KIT_ROOT/tools`.
- For `tools/lib/collab_common.py`: `SCRIPT_DIR == KIT_ROOT/tools/lib`.

So Python must **not** blindly use `Path(__file__).resolve().parent` as `KIT_ROOT`. `resolve_kit_root()`
**walks upward** from `SCRIPT_DIR` looking for the kit-root signature — a directory containing both
`tools/` and `install.sh` (or an explicit `.collab-kit` marker) — bounded to ≤6 levels, and raises
`CollabError` if not found. This is layout-refactor-tolerant and works for shim *and* core alike.

- **Symlinks:** `SCRIPT_PATH` canonicalizes via `Path(__file__).resolve()` (Python) / `readlink -f "$0"`
  (bash) / `$PSCommandPath` + `Resolve-Path` (PowerShell) — required because `install.sh` symlinks
  `bin/*` into `~/bin`, so the CLI is invoked *through* a symlink and must still locate `KIT_ROOT`.
- **`COLLAB_HOME`** is canonicalized with `.resolve()` so two spellings of the same dir can never map
  to two different lock dirs (a correctness issue, not cosmetics). Resolution order:
  env → shell-rc value → `KIT_ROOT`.
- **`collab_common.sh`** performs the same upward walk in bash; it owns path resolution *only*.
- **Invocation table (documented in the module docstring):**

  | Invocation | `SCRIPT_DIR` | `KIT_ROOT` (via upward walk) |
  |---|---|---|
  | `python3 tools/lib/collab_common.py` | `…/tools/lib` | `…/` (root) |
  | `python -m tools.lib.handoff_core` | `…/tools/lib` | `…/` (root) |
  | `~/bin/handoff` (symlink → `tools/handoff` → core) | `…/tools` (real, post-symlink) | `…/` (root) |
  | PowerShell `& handoff …` | `…/tools` | `…/` (root) |
  | run from an unrelated CWD | unchanged | unchanged (walk is `__file__`-relative, never CWD-relative) |

### 3. Atomic write robustness (Windows) — RESOLVED

`os.replace()` is atomic but on Windows raises `PermissionError` (WinError 5/32) when the target is
open by a reader (watchers poll files). Split into two functions:

```
atomic_write(path, data):           # low-level; may raise PermissionError on a locked target
  tmp = f"{path}.tmp.{pid}.{time_ns}"
  write(tmp, data); os.replace(tmp, path)

safe_write(path, data, *, retries=5, backoff=0.02):   # common case
  for attempt in range(retries):
    try: atomic_write(path, data); return
    except PermissionError:                            # transient reader lock only
      sleep(backoff * 2**attempt); continue
    # any other OSError -> re-raise immediately (do not mask real failures)
  raise CollabError(f"could not replace {path} after {retries} tries; a reader may hold it open")
```

- **Bounded**, never infinite — a reader holding a handle forever fails loudly, never hangs.
- **Reader convention (documented, enforced by code review of higher slices):** readers do
  `Path(...).read_text()` (open-read-close), never hold handles across work, minimizing collisions.
- Registry/handoff writers use `safe_write`; the low-level `atomic_write` is available where the
  caller wants to handle contention itself.

### 4. Error handling & diagnostics — RESOLVED

Exception hierarchy (documented `Raises:` on every public function):

```
CollabError(Exception)            # base
├── LockTimeout(CollabError)      # acquire exceeded acquire_timeout
└── LockBroken(CollabError)       # our lock was broken (op ran past ttl); raised on release
slugify(...) -> ValueError        # empty / fully-stripped input
```

Lock handle exposes `.path` and `.acquired_at`; `meta.json` records `{pid, host, acquired_at, ttl}`
for post-mortem debugging. Raw `OSError`/`FileNotFoundError` never bubble unwrapped out of the lock
API.

### 5. Tests with this slice — RESOLVED, strengthened

A concurrency primitive ships with a concurrency test — not deferred to slice 2:

- **`test_lock_mutual_exclusion`** — spawn N (≥8) `multiprocessing` workers contending for one lock,
  each increments a counter inside the critical section; assert final count == N and no interleave
  (exactly-one-holder invariant).
- **`test_stale_break_single_winner`** — plant a stale lock dir (backdated mtime), race M breakers;
  assert exactly one `broke stale lock` warning and one final holder.
- **`test_lockbroken_on_release`** — hold past ttl, have another process break it, assert `LockBroken`
  on release.
- **`test_atomic_write_no_partial`** — kill mid-write (or inject failure before `os.replace`); assert
  target is either old-complete or new-complete, never truncated.
- **`test_safe_write_retries_then_gives_up`** — simulate persistent `PermissionError`; assert bounded
  retries then `CollabError`.
- **`slugify` table/property tests** — `"../../etc/passwd"`→ safe non-traversing slug; `".."`→
  `ValueError`; `"café"`→`"cafe"`; `"  "`→`ValueError`; result always matches `^[a-z0-9][a-z0-9-]*$`.
- Doctests for `resolve_kit_dir` / `resolve_collab_home` happy paths.

## Commit defense (reviewer extra requirement) — belt AND suspenders

Every lock-protected *final mutation* uses two independent defenses, not just the coarse lock:

1. **Coarse lock + fencing** — `collab_lock(...)` held, `handle.assert_current()` called immediately
   before the mutation.
2. **Optimistic commit primitive** — the mutation itself uses an atomic, collision-detecting op:
   - new-file creation (e.g. handoff id allocation, slice 2): `exclusive_create(path, data)` →
     `os.open(path, O_CREAT | O_EXCL | O_WRONLY)`; on `FileExistsError`, re-allocate the next id and
     retry. This is the second line of defense if a stale holder resumes after TTL.
   - in-place replace (registry): `safe_write` (atomic `os.replace`).

`collab_common.py` provides `exclusive_create`; slice 2 (id allocation) is required to use pattern 1+2
together. Recorded here because the primitive lives in this slice.

## Parallel adversarial verification (gate for the CODE diff, not this handoff)

Because this slice touches the `concurrency` guardrail, the *implementation diff* gets independent
adversarial coverage across five lanes before it becomes slice 2's foundation:

| Lane | Target |
|---|---|
| Lock-model | prove acquire/break/release + fencing cannot yield two valid owners of the same canonical token |
| Windows-path | Git Bash, PowerShell, symlinked `~/bin`, direct, `python -m` |
| Atomic-IO | injected replace failures, temp-file leftovers, watcher-open targets, crash-before-replace |
| Path-safety | `slugify` traversal, Unicode, reserved Windows names (`CON`, `NUL`, …), empty, collisions |
| Concurrency-stress | multiprocess contention, stale-break race, slow-holder-resumes-after-break, release-token mismatch |

Run via the regression-hunt workflow / parallel subagents on the diff. **Reserved Windows device
names** (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`) are added to `slugify`'s reject/mangle set
per the path-safety lane.

## Accepted minors

- **TTL per call site** — `collab_lock(..., ttl=...)` (already in the signature).
- **`is_lock_held(lockdir) -> bool`** — added, but documented **advisory-only** (inherently TOCTOU;
  for diagnostics/watcher display; MUST NOT gate control flow — use `collab_lock` for that).
- **`.sh` is thin** — enforced by the scope table above.

## Risks & questions for reviewer

- Is the **ttl-break contract** acceptable for our single-machine, multi-session model, or do you
  want a heartbeat/renew mechanism now (adds complexity to slice 1)? My recommendation: ttl +
  per-call-site override now; defer heartbeat unless a real long-op needs it (YAGNI).
- `acquire_timeout` default of 60s — reasonable for agent-paced ops, or too long/short?
- Any objection to `mtime` (not `meta.json`) as the authoritative staleness clock? (Chosen to avoid a
  read-during-write race on the metadata.)

## Implementation & five-lane verification results (builder → reviewer)

Implemented at `collab-kit/tools/lib/collab_common.py` + `.sh`, tests at
`collab-kit/tests/test_collab_common.py`. Five adversarial lanes were run on the diff. **Two
independent lanes converged on a HIGH release defect** (high confidence). All confirmed findings
fixed; **24/24 tests pass** (incl. multiprocess contention, stale-regime-with-fencing, and the
deterministic fenced-release restore path). Ledger:

| # | Lane(s) | Severity | Finding | Resolution |
|---|---|---|---|---|
| 1 | lock-model **+** concurrency-stress | HIGH | Release was TOCTOU: check-token then `rmtree` non-atomic → could delete a *newer* owner's lock in place → ME break | **rename-capture release** (`_fenced_release`): atomic capture → verify-ours → rmtree, else restore. Deterministic unit test + stale-regime multiprocess test |
| 2 | concurrency-stress | HIGH | Break path had the same TOCTOU: 2nd breaker renames away a freshly-acquired lock | **capture-verify break**: after rename, re-check captured mtime; if fresh, restore + wait |
| 3 | windows-path | HIGH | MSYS `/c/..` `COLLAB_HOME` handed to native Python → `C:\c\..` → bash-shim vs direct-Python compute **different lock dirs** | Python `_normalize_msys_path`; `.sh` no longer exports MSYS-form `COLLAB_HOME` to the child. Tested |
| 4 | windows-path | HIGH | Symlinked `~/bin` shim: `readlink -f` doesn't follow MSYS links. **Root cause found: `ln -s` on this Git Bash copies / makes unfollowable links** | Runtime symlink resolution abandoned on Windows; **`COLLAB_KIT_ROOT` env override** (both cores) that `install.sh` embeds at install time. Closed + tested |
| 5 | atomic-io | LOW-MED | temp leak if `write_bytes` fails; temp-name collision; `safe_write` dropped error context / `retries<=0`; no `fsync` on id-commit | `atomic_write` wraps write+replace; `+urandom` temp suffix; `raise … from last` + `max(1,retries)`; `fsync` in `exclusive_create` |
| 6 | path-safety | LOW | reserved-name mangle self-collided (`con`/`x-con`) | prefix `reserved--` (double-dash impossible in a normal slug → collision-free). No path-escape found — slugify cleared |

**Residuals (documented, accepted):** (a) an extraordinarily rare "capture-foreign-then-can't-restore"
race leaks a graveyard dir but never lets two processes mutate (the foreign owner's `assert_current`
fails safe); (b) `collab_root` is a flat namespace — cross-user isolation must rest on the registry,
not slug uniqueness (constraint recorded for the Telegram-bridge slice); (c) DEFECT B's robust fix
requires `install.sh` (slice 8) to embed `COLLAB_KIT_ROOT`.

## Verification round 2 — `exclusive_create` commit primitive (five lanes)

Reviewer required replacing `exclusive_create` with a staged-durable-write + no-overwrite hard-link
commit, the 7 commit tests, and five parallel verification lanes. Done. `os.link` no-overwrite +
unchanged-destination semantics were empirically verified on this NTFS box first. Lane results:

| Lane | Verdict | Action |
|---|---|---|
| no-overwrite | NO DEFECT — `final` only ever an `os.link` dest; existing dest byte-unchanged; `FileExistsError` handler precedes generic `OSError` | — |
| commit-atomicity | NO DEFECT — `final` never an `O_CREAT` target; `_write_all` loops correctly; fd closed on all paths | — |
| filesystem-residual | NO DEFECT — temp names (`.tmp.<hex>` tail) can't collide with `*.md` records; leaks bounded to the documented residual | — |
| **windows** | **CONFIRMED HIGH** — `os.open` without `O_BINARY` → text-mode fd → `os.write` translates `\n`→`\r\n`, silently corrupting every newline-bearing record | **Fixed:** `\| getattr(os,"O_BINARY",0)`; added byte-exact newline regression test (the gap that let it slip) |
| state-machine preview | COMPOSITION SOUND (scenario A: 120 unique/contiguous/complete ids, 0 leaks) + 3 guardrails | Gap 3 fixed in `collab_lock`; Gaps 1–2 recorded as slice-2 constraints below |

**Also fixed (defense-in-depth from lane cross-findings):**
- `collab_lock` **Gap 3**: the `mkdir`→`meta.json` window was unfenced — a breaker renaming the
  lockdir away made `atomic_write` raise an unhandled `FileNotFoundError` (crash). Now caught →
  **re-compete** (regression-tested). Only reachable at `ttl` ≪ critical section.
- `_fsync_parent_best_effort` and fd closes now **never raise post-publish** (`_close_quietly`), so a
  best-effort fsync hiccup can't make a committed record look failed.

**Slice-2 composition constraints (MUST honor — from the state-machine lane):**
1. **Id-uniqueness key.** `exclusive_create`'s exclusivity is keyed on the *full path*. To use it as
   a real backstop against a broken lock, key the commit on the **id alone** (reserve
   `pending/{id:03d}.md` before the slugged file) — else two racers with the same id but different
   slugs both publish. Under a correct `collab_lock` this never arises; the lock is the primary
   guarantor.
2. **No post-commit retry.** Never retry a `LockBroken` raised *after* the `exclusive_create` commit
   (it double-commits). Retry only pre-commit `LockBroken`/`assert_current` failures.
3. **`ttl` > critical section.** Keep the id-allocation section short and `ttl` comfortably above it.

**Tests: 32 passed** (25 core + 7 commit-primitive), clean import.

## Request

Requesting reviewer sign-off on the **actual diff** (`collab-kit/tools/lib/collab_common.{py,sh}`
+ tests). The approval-bar invariant is enforced (fenced `assert_current` + rename-capture release);
the five-lane verification is complete with all confirmed findings fixed and tests green. On sign-off
this slice becomes the foundation for slice 2 (the handoff state machine / id allocation), which is
required to use `collab_lock` + `assert_current` + `exclusive_create` together per the commit-defense
section. No heartbeat, no daemon, no third-party packages, no duplicated shell locking.
