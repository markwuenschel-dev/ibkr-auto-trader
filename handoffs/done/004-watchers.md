---
to: reviewer
from: builder
id: 004-watchers
title: "Handoff watchers (collab-kit slice 4)"
priority: normal
date: 2026-07-04
status: done
approved: 2026-07-04 (independent reviewer; production-grade watcher lanes passed; B1-B5 blockers resolved)
guardrails: [concurrency, data-integrity]
---

## Summary

Slice 4 adds the **watchers** (architecture §A6): a persistent, stdlib-only poller that tails a
collab's `handoffs/pending/` and surfaces a new handoff *addressed to this seat* the moment it
appears — so the reviewer gets pinged when the builder requests a review, and vice versa. Plus an
optional cross-collab `watch-all` that fans new-handoff events across every registered collab. This
closes the loop that, today, requires a human to notice a new file by hand.

## Constraints

Carries forward (must remain true): [C7] [C17] from prior handoffs.
New to slice 4:

- [C20] the watcher is **read-only** over `handoffs/` — it never mutates handoff state; it only writes its own seen-set
- [C21] the seen-set is **persisted** (`logs/watch-<seat>.state`) and written atomically (`safe_write`), so a restart never re-announces a handoff it already surfaced
- [C22] **polling** only (stdlib, no inotify/ReadDirectoryChangesW dependency — those differ per OS); interval configurable, default 2s ([architecture §A6])
- [C23] surfaces a handoff only when its frontmatter `to:` matches this watcher's **seat** (builder-side vs reviewer-side); other seats' handoffs are ignored
- [C24] a malformed/unparseable file in `pending/` must **not crash the loop** — skip it (once), warn, keep watching
- [C25] one parameterized implementation (`watcher.py --seat <seat>`); the architecture's `watch-for-claude` / `watch-for-grok` names are thin wrappers, not duplicated logic (cf. [C14])

## Details

**`watcher.py`** — `watch(collab, *, seat, interval=2.0, once=False)`:
1. Resolve `pending/` via `handoff_core` layout; load the persisted seen-set from `logs/watch-<seat>.state`.
2. Each tick: `list_handoffs(collab, "pending")`; for each not in the seen-set, `contracts.parse_handoff` it, check `frontmatter["to"] == seat`; if so, print an in-session banner and (optionally) emit a `watch` trace event; add id to the seen-set and persist.
3. `--once` runs a single tick (testable); default loops every `interval`.

**`watch_all.py`** — iterate `registry.load()`'s collabs, run one tick per collab, fan matching events to the notification sink (stdout now; Telegram bridge in slice 5). Reuses `registry.status`-style iteration.

**Reused:** `handoff_core.list_handoffs`, `contracts.parse_handoff`, `collab_common.safe_write` (atomic seen-set), `registry.load` (watch-all), `trace.emit` (optional watch events, `_emit_safe`-wrapped).

## Risks & Questions for Reviewer

- Seen-set keyed by handoff **id** (not filename) — an id that re-appears after moving out of pending is not re-announced. Correct, or should it key on (id, first-seen-mtime)?
- Should the watcher surface a handoff that was *already* pending at startup (cold start), or only genuinely-new ones after it starts? Proposed: on first run, seed the seen-set with existing pending (don't announce backlog), announce only new arrivals — with a `--catch-up` flag to override.
- `watch-all` notification sink is stdout for now; the Telegram path lands in slice 5.

## Implementation & verification (builder → reviewer) — PRODUCTION GRADE

Implemented `watcher.py` + `test_watcher.py` (17 tests, 1 FIFO test skipped on Windows). Verified in
**two rounds**: an initial 3-lane pass, then — per reviewer direction that a control-plane component
warrants more — **5 blockers fixed** and the **full five watcher lanes** run.

**Reviewer blockers (all fixed + regression-tested):**

| # | Blocker | Fix | Test |
|---|---|---|---|
| B1 | `_persist_merge` wrote under the lock without fencing (slice-1 invariant) | `h.assert_current()` before the commit | `test_persist_merge_fences_before_commit` |
| B2 | corrupt state file re-announced the backlog ([C21] break) | `_read_state` distinguishes corrupt; quarantine + re-seed | `test_corrupt_state_reseeds_not_reannounces` |
| B3 | duplicate-announce with concurrent watchers | **at-least-once** made the explicit documented contract (no lost-update, no re-announce) | concurrent test + docstring |
| B4 | non-regular files (FIFO/device/symlink/oversized) parsed | `lstat`+`S_ISREG` only, + 512 KB size cap | FIFO + oversized + symlink tests |
| B5 | wrong-seat handoff marked seen → retarget never announced | **routing-immutability** made the explicit contract | `test_routing_immutable_retarget_not_announced` |

**Five-lane adversarial verification — all clean:**

| Lane | Verdict |
|---|---|
| 1 durability/concurrency | **all SAFE** — lost-update closed (8 watchers → all persisted); **B1 fence proven via a real cross-process stale-break** (`_save_seen` never called under a lost lock); B2/B3/restart confirmed |
| 2 adversarial pending-file | **NO DEFECT** — symlink/FIFO/dir/oversized skipped pre-parse; invalid-UTF-8/unreadable → warn-once; legit + at-cap announced |
| 3 seat-routing | **PASS 22/22** — case/space-insensitive, no over-match leak, cold-start/catch_up/retarget correct |
| 4 watch-all degradation | **SAFE** — corrupt registry → clean exit 1; bad collab contained. One tidiness note fixed (`is_dir` guard: don't materialize a missing root) |
| 5 dogfood (CLI→watcher) | **PASS 9/9** — end-to-end pipeline, telemetry + state artifacts correct, read-only preserved |

**Earlier 3-lane findings** (case-filter, symlink, merge-under-lock, corrupt-crash, watch-all traceback)
are all subsumed by the blocker fixes above. **17/17 watcher tests, 109 full-suite pass.** (Linux CI
leg recommended to exercise FIFO / real `0o000` natively — covered here by green branches only.)

## Closeout

**APPROVED (independent reviewer, 2026-07-04) — moved `pending/` → `done/`.** The five B1–B5 blockers
are resolved and the full five-lane production-grade verification passed (durability/concurrency,
adversarial pending-file, seat-routing, watch-all degradation, dogfood). The operational semantics are
now explicit contracts: at-least-once delivery (one watcher per seat to avoid dupes), routing-immutable
after creation, corrupt-state quarantine+re-seed, regular-bounded-file parsing only, and fenced
seen-state persistence. Follow-up: a Linux CI leg to exercise FIFO / real `0o000` natively (covered
here by green branches only). Slice 5 (Telegram bridge) is next.
