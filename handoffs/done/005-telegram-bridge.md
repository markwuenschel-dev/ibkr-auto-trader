---
to: reviewer
from: builder
id: 005-telegram-bridge
title: "Telegram bridge (collab-kit slice 5)"
priority: normal
date: 2026-07-04
status: pending
guardrails: [path-safety, data-integrity, auth]
---

## Summary

Slice 5 adds the **phone bridge** (architecture §A7): a zero-dependency (`urllib` long-poll) adapter
over a dead-simple **file protocol** that puts the human in the loop over Telegram. Agents write to an
outbox and the bridge forwards to your chat; you reply `/c <project> <message>` and the bridge drops it
into an inbox the watcher surfaces. Telegram is **optional** — the core handoff loop works without it —
and the bridge is a swappable adapter (Slack/Discord drop in the same way).

## Constraints

Carries forward (must remain true): [C3] (slug path-safety) [C7] (stdlib) from prior handoffs.
New to slice 5:

- [C26] **zero third-party deps** — stdlib `urllib` long-poll only (no `requests`); bring-your-own @BotFather token via `TELEGRAM_BOT_TOKEN`
- [C27] **chat-id learn-and-lock** — the first inbound message learns and locks the chat id (`logs/telegram-chat.lock`); `TELEGRAM_CHAT_ID` set explicitly overrides (public bots); the bridge answers exactly one chat
- [C28] **path-traversal defense** — `/c <project>` is slug-sanitized (`collab_common.slugify`) before it becomes a path; an **unknown project is rejected, not created** (validated against the registry)
- [C29] **at-least-once outbound** — an outbox file is archived **only after** Telegram returns `ok`, so a crash mid-send re-sends rather than drops
- [C30] **Telegram is optional** — the file protocol (outbox/inbox) is the interface; the bridge is a separable adapter, and the handoff loop runs without it
- [C31] **swap-able sink** — the core never imports the bridge; Slack/Discord/etc. are drop-in adapters over the same outbox/inbox files
- [C32] **outbound is path-safe** — `send_outbox` forwards only *regular* `outbox/*.md` files (`lstat`+`S_ISREG`): symlinks (info-disclosure), FIFOs/devices/dirs are skipped, and content is read bounded (no unbounded slurp). Mirrors the watcher's `pending/` rule (B4) for the outbox boundary.
- [C33] **inbox drain is single-consumer + at-least-once** — `drain_inbox` runs under a per-project lock (`.drain.lock`) with an `assert_current` fence before the archive move, and skips non-regular/oversized files. Single-consumer in the normal case; at-least-once across a crash (re-surface once). This is the human-control channel, so it gets the same lock-fenced discipline as every other final mutation.

## Details

**File protocol** (under `$COLLAB_HOME`):
- agent → phone: write `outbox/<ts>-<project>.md` → bridge sends to the locked chat → on confirmed delivery moves it to `outbox/archive/`.
- phone → agent: you send `/c <project> <message>` → bridge writes `inbox/live/<project>/from-user-<ts>.md` → that collab's watcher (slice 4) surfaces it.

**`telegram_bridge.py`** (stdlib):
- `send_outbox(home, token, chat_id)` — scan `outbox/*.md`, `sendMessage` via `urllib`, archive on `ok` ([C29]).
- `poll_updates(home, token)` — `getUpdates` long-poll (persisted `offset` in `logs/telegram-offset`); parse `/c <project> <msg>`, `slugify(project)`, validate against the registry, write the inbox file (else reject) ([C28]); learn+lock the chat id on first inbound ([C27]).
- `run(home, ...)` — alternate send + poll; `--once` for tests.

**Reused:** `collab_common.slugify`/`safe_write`/`collab_lock` (chat-id lock), `registry.load` (known projects), `resolve_collab_home`. Network layer is `urllib.request` only.

## Risks & Questions for Reviewer

- Project validation for `/c`: I plan to require the project be a **registered** collab (registry) — reject unknown. Or should an unregistered-but-existing collab dir be allowed? Proposed: registry-only (tightest).
- `getUpdates` offset persistence (avoid reprocessing) — persist last `update_id` in `logs/`; acceptable?
- Testing: the Telegram HTTP layer will be **mocked** (`urllib` patched) — no live bot token in CI. The file protocol, chat-id lock, slug/traversal defense, and at-least-once archival are the real tested surface; a live smoke test needs your bot token.

## Verification status — COMPLETE (5 adversarial lanes run; all confirmed defects fixed)

Implemented `telegram_bridge.py` + `test_telegram_bridge.py` (`urllib` fully mocked, no network). **Full
suite green: 135 passed / 5 skipped (bash-shim skips) / 23 subtests.** Both items that previously blocked
sign-off are now closed: the five lanes ran to completion with executable repros, and inbox consumption
is wired (§A6).

### Adversarial ledger — 5 lanes, each with runnable repros in the scratchpad

**Lane 1 — auth / chat-id-lock — 2 CONFIRMED defects, both FIXED:**
- **DEFECT 4 (race):** first-inbound learn used a non-atomic `if not lock.exists(): safe_write(...)` →
  two concurrent first-inbound messages were last-writer-wins (attacker could win the lock). **Fix:**
  `cc.exclusive_create` (atomic first-writer-wins); `except FileExistsError: return`. Regression:
  `test_learn_is_first_writer_wins`.
- **DEFECT 5 (auth bypass + owner lockout):** a spoofed `{"chat":{"id":""}}` wrote an *empty* lock →
  `resolve_chat_id` returned `None` → the one-chat filter was **disabled** (everyone routed) and the real
  owner was locked out. **Fix:** reject any non-`int`/zero/bool chat id (`if not (isinstance(chat, int)
  and not isinstance(chat, bool) and chat != 0): continue`) and never route while `locked is None`.
  Regression: `TestSpoofedChat` (parametrized `''`, `0`, `None`, `[1]`, `True`, missing).

**Lane 2 — malformed / untrusted `getUpdates` — 1 CONFIRMED crash, FIXED:** a directly-reproduced daemon
crash on non-int `update_id`, non-list `getUpdates`, non-dict update, non-dict chat, non-string text is
now skipped defensively, and `run()` catches broadly so untrusted network input can never kill the
daemon. Regressions: `test_malformed_updates_never_crash`, `test_non_list_getupdates_no_crash`.

**Lane 3 — at-least-once / archival / concurrency — 2 CONFIRMED defects FIXED, 1 minor FIXED, 3 ACCEPTED:**
- **#2 double-send + #3 offset-rollback (CONFIRMED, FIXED):** two bridges on one home double-send outbound
  and race `_save_offset` **backwards** (replaying inbound). Root cause is one invariant — *one bridge per
  home* — which was only documented. **Fix:** `run()` now takes an **OS-level exclusive lock**
  (`logs/telegram-bridge.lock`, `msvcrt`/`fcntl`) held for the process lifetime; a second bridge fails
  fast with exit 1. Chosen over a PID-file (stale-forever after crash) and over `collab_lock` (its 30s TTL
  is shorter than the bridge's own 50s `getUpdates` long-poll → would go stale mid-normal-operation); the
  kernel releases an OS lock on crash, so neither problem exists. Regression: `TestSingleton`.
- **#5 silent truncation (minor, FIXED):** a >4096-char message was silently clipped. **Fix:** truncate
  for the phone with a visible `…[truncated N chars — full text in outbox/archive/…]` marker; the full
  text is always preserved in the archive. Regression: `test_oversized_message_truncated_with_marker`.
- **ACCEPTED as designed:** #1 archive-fail re-send (the [C29] at-least-once guarantee); #4 clobbered
  offset fails safe to 0 and replays Telegram's bounded (~24h) backlog once — a duplicate flood, never a
  crash/drop; #6 partial outbox read — the bridge has no write-completion signal, so atomic publish is
  the agent's responsibility. All three are now stated explicitly in the module docstring.

**Lane 4 — untrusted inbox body (DoS / injection) — DEFECT 1+2 CONFIRMED, FIXED:** the `/c` body was
written unbounded and un-sanitized → disk-fill DoS and injection of forged structure (`## Constraints`),
NUL bytes, and terminal escapes into a file another agent parses. **Fix:** cap at `_MAX_MSG` and drop
control chars incl. embedded newlines (a `/c` message is one line). Regression:
`test_inbox_body_bounded_and_decontrolled`.

**Lane 5 — path-traversal / routing-immutability — defense CONFIRMED holds:** `/c ../../etc` → slug
`etc` → not registered → rejected, nothing escapes `$COLLAB_HOME`; an unknown project is **rejected, not
created** ([C28]). Regressions: `test_traversal_in_project_neutralized`, `test_unknown_project_rejected`.

### Inbox-consumption wire (§A6) — DONE

`watcher.drain_inbox(home, project, *, seat)` surfaces and consumes `inbox/live/<slug>/from-user-*.md`
(prints the message, emits a telemetry route event, moves it to `inbox/live/<slug>/archive/`). Wired into
both `watch()` (per-collab) and `watch_all()`. Non-regular files skipped; unreadable files skipped;
archive move is best-effort. Regression: `TestInboxDrain` in `test_watcher.py`.

### Documented residuals (single-bridge contract, now ENFORCED)

At-least-once outbound (#1) and trust-on-first-use auth ([C27]) are intentional; TOFU is now enforced
against spoofing (Lane 1) and a startup warning is logged when no chat is pinned. The "one bridge per
home" rule is no longer advisory — it is enforced by the singleton lock (Lane 3). For a public bot, pin
`TELEGRAM_CHAT_ID`.

## Round 2 — reviewer blockers (all addressed)

You rejected round 1 with two production-grade blockers + one hardening item, reviewing the watcher's
new inbox-drain as part of 005 (correct — it's slice-5 behavior). All three verified against the code
and fixed; full suite **141 passed / 5 skipped / 23 subtests**.

**Blocker 1 — `send_outbox` followed symlinks / non-regular / unbounded files (CONFIRMED, FIXED).**
Your repro (symlinked `outbox/*.md` → `secret.md` sent as its target contents) is real: `send_outbox`
did a bare `read_text()` with no `lstat`/`S_ISREG`/size guard — the exact class the watcher already
fixed for `pending/`, never applied to the outbox. **Fix ([C32]):** `lstat` + `S_ISREG` reject
symlinks/FIFOs/devices/dirs (skip in place, warn once); content read bounded to `_READ_CAP` (256 KiB) so
a planted multi-GB file can't be slurped. Regressions: `test_outbox_symlink_skipped_not_sent`,
`test_outbox_nonregular_skipped`, `test_outbox_oversized_file_does_not_read_unbounded` (symlink test runs
for real here, not skipped).

**Blocker 2 — `drain_inbox` claimed exactly-once but was at-least-once (CONFIRMED, FIXED via Option A).**
Right: the code commented "surfaced exactly once" with no lock, so two live drainers
(`watch --collab X` + `watch --all`) both read+print before either `os.replace`. I took **Option A
(single-consumer lock)** over Option B (relabel at-least-once) because this is the human-control channel
and it matches collab-kit's invariant that every final mutation is `collab_lock` + `assert_current`
fenced (same shape as `_persist_merge` directly above it). **Fix ([C33]):** the drain runs under a
per-project `.drain.lock` (short 2 s acquire → a peer holding it means it's draining, so skip this tick,
no stall) with `assert_current` before the archive move. Contract worded **honestly**: single-consumer
in the normal case, at-least-once across a crash (crash after print before move re-surfaces once) — the
same delivery guarantee as handoff watching, no longer a false exactly-once claim. Regression:
`test_inbox_single_consumer_lock_skips_when_held` (proves a held lock → skip → no drop).

**Hardening — inbox drain had no pre-read size cap (FIXED).** A local process could drop a huge
`from-user-*.md`. **Fix:** `lstat` size check against `_MAX_INBOX_BYTES` (64 KiB) before `read_text`,
plus the same `S_ISREG` guard. Regressions: `test_inbox_oversized_file_skipped_or_bounded`,
`test_inbox_symlink_skipped`.

**Acknowledged-good (your review):** the `exclusive_create` chat-lock, the `poll_updates` untrusted-input
rejection, and the singleton bridge lock — all unchanged.

## Round 3 — "source/test mismatch" flag (verified: false alarm)

A later review flagged 005 as blocked for a source/test mismatch — claiming the on-disk `send_outbox`
still did a bare `read_text()` that follows symlinks. **Verified against the actual repo: it does not.**
`C:\Users\Nalakram\Documents\GitHub\collab-kit\tools\lib\telegram_bridge.py` has the `lstat`+`S_ISREG`
guard and the bounded `_READ_CAP` read (lines ~154–164), and `python -m pytest tests/test_telegram_bridge.py`
passes (now **28 passed / 1 skipped**). The mismatch was an artifact of reviewing a pasted snippet rather
than the source tree. No source change was needed there.

Adopted the review's stronger asks anyway (they harden coverage): `_READ_CAP` raised to 512 KiB; added
`test_outbox_fifo_skipped_if_platform_supports_fifo` (POSIX-gated), `test_outbox_invalid_utf8_decodes_replace_not_crash`,
and `test_archived_only_after_confirmed_send`.

**Config convenience (new):** `run()` now calls `cc.load_dotenv()` at startup, so `TELEGRAM_BOT_TOKEN`
(and `TELEGRAM_CHAT_ID`) can live in `<kit root>/.env` instead of a shell export — one secrets file shared
with the autopilot adapter. `load_dotenv` is stdlib, best-effort (missing file = no-op), and uses
`setdefault` so a real exported env var always wins. Tests: `TestLoadDotenv` (collab_common) +
`test_run_loads_token_from_dotenv` (bridge).

## Request

Round-2 blockers cleared: outbound path-safety ([C32]), single-consumer inbox drain with an honest
at-least-once contract ([C33]), and the inbox size cap — each with regressions. The "source/test mismatch"
was verified a false alarm (round 3). Full suite green at **163 passed / 6 skipped / 23 subtests**. The
watcher inbox-drain is in-scope and covered. Re-requesting **production-grade sign-off on 005**. I have
**not** moved this handoff to `done/` — that transition remains yours on approval.
