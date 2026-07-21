# collab-kit — Architecture & Build Spec

> Status: **Design (pre-implementation)**. This is the authoritative, fleshed-out design for the
> collab-kit orchestration layer. No production code is written until each slice is proposed as a
> handoff and passes independent review (see [`README.md`](./README.md) → bootstrap process).

collab-kit is a **file-based orchestration layer** for running a *builder + independent-reviewer*
agent loop over a codebase, with a human reachable over Telegram. It treats agent output as
untrusted code and makes verification a first-class, parallel, independent step.

---

## 1. Design tenets

| Tenet | Consequence in the design |
|---|---|
| Plain files, no daemon | All state on disk → inspectable, crash-safe, resumable. No in-memory authority. |
| Per-project isolation | Each collab owns its `handoffs/`, `logs/`, `context/`, locks. Concurrent collabs never race. |
| Agent-agnostic | Any CLI agent that reads/writes files + runs shell can occupy a seat. Value = review depth, not agent count. |
| Zero third-party Python | stdlib only: `json`, `pathlib`, `argparse`, `urllib`, `os`, `re`. |
| Self-locating scripts | `KIT_DIR` from the script's own resolved path; `COLLAB_HOME` env-overridable. Runs under any user/path. |
| One logic, two dialects | State-machine logic lives once in a stdlib-Python **core**; Bash + PowerShell are thin shims. |

---

## 2. Repository & data layout

### 2.1 The kit (code)

```
collab-kit/
  bin/
    newproject                 # scaffold + register a collab
    restart                    # re-print the session bootstrap block for an existing collab
  tools/
    handoff                    # CLI shim (bash)      → exec lib/handoff_core.py
    handoff.ps1                # CLI shim (PowerShell) → exec lib/handoff_core.py
    collab-handoff             # per-collab wrapper: binds HANDOFF_ROOT then calls handoff
    watch-for-claude-handoffs.py
    watch-for-grok-handoffs.py
    watch-all-handoffs.py
    telegram-bridge.py
    diff-regression-hunt.workflow.js
    lib/
      handoff_core.py          # THE state machine + registry logic (single source of truth)
      collab_common.py         # KIT_DIR/COLLAB_HOME resolution, atomic IO, locking (python)
      collab_common.sh         # same resolution helpers for bash shims
  skills/collab/SKILL.md       # Claude Code /collab front door
  install.sh
  collabs.json.example
```

### 2.2 The data root (`$COLLAB_HOME`)

```
$COLLAB_HOME/                  # defaults to the kit dir; override via env / shell rc
  collabs.json                 # registry (§4)
  <name>/                      # one isolated collab
    handoffs/
      pending/  claimed/  done/  archive/
    context/                   # IDEA.md + domain files (e.g. CONTEXT.md)
    logs/                      # watcher/bridge state, locks (gitignored)
      handoff.log
      watch-<seat>.state
      .lock/                   # coarse lock dir (atomic mkdir)
    PROTOCOL.md  REVIEWER-BRIEFING.md  KICKOFF.md
  outbox/                      # agent → Telegram queue
    archive/                   # delivered messages
  inbox/live/<project>/        # Telegram → agent queue
  logs/                        # global bridge/watcher state
    telegram-chat.lock
```

### 2.3 Path resolution

- `KIT_DIR`: bash `dirname "$(readlink -f "$0")"`; PowerShell `$PSScriptRoot`; Python
  `Path(__file__).resolve().parent`. Always derived from the script itself.
- `COLLAB_HOME`: (1) explicit `$COLLAB_HOME` env → (2) value `install.sh` wrote to the shell rc →
  (3) fallback to `KIT_DIR`. Resolved identically in all three dialects via `collab_common.*`.

---

## 3. The dual-dialect CLI over a Python core **[DECIDED]**

The dev box is Windows 11 + Git Bash, and the CLI must run in **native PowerShell too**. To avoid
maintaining the state machine twice:

- `tools/lib/handoff_core.py` — a stdlib-only Python program implementing **every** subcommand
  (parse args → mutate files → print → exit code). This is the only place the logic exists.
- `tools/handoff` (bash) and `tools/handoff.ps1` (PowerShell) — ~20-line shims that resolve
  `KIT_DIR`/`COLLAB_HOME` in their dialect, then `exec python3 "$KIT_DIR/lib/handoff_core.py" "$@"`.

Result: PowerShell and Git Bash users get identical behavior; the "double maintenance" is only the
two trivial shims, never the logic or the locking.

---

## 4. Registry — `collabs.json`

```json
{
  "version": 1,
  "collabs": {
    "ibkr-auto-trader": {
      "root": "/c/Users/Nalakram/collab-home/ibkr-auto-trader",
      "repo": "git@github.com:me/ibkr-auto-trader.git",
      "reviewer": "grok",
      "created": "2026-07-03",
      "guardrails": ["money", "safety", "data-integrity"]
    }
  }
}
```

- **Writers:** only `newproject` and `handoff register`. Every write is atomic —
  write `collabs.json.tmp`, `os.replace()` over the original — so a crash never yields a half-written
  registry.
- **`guardrails`** drive the risk-trigger for the regression hunt (§8) and are written *fresh per
  project* by `newproject` (never inherited from another collab).
- **`root`** is absolute; the registry is the map from collab name → everything else.

---

## 5. Handoff file format & id scheme

Markdown with YAML frontmatter. Superset of the existing `001-initial-design.md`:

```yaml
---
to: reviewer            # seat name ("reviewer"/"builder"), not a person
from: builder
id: 001-initial-design  # NNN-slug
title: "Clear descriptive title"
priority: high | normal | low
date: YYYY-MM-DD
status: pending | claimed | done   # CREATION-TIME metadata only; the DIRECTORY is sole truth
---

## Summary
## Details
## Risks & Questions for Reviewer
## Request
```

- **id allocation:** `NNN = 1 + max(numeric prefix across ALL FOUR state dirs)`, zero-padded to 3.
  Scanning all four dirs (not just `pending/`) means ids never collide even after files move.
- **slug:** sanitized to `[a-z0-9-]`, derived from `--title`.
- **`status` field** is **creation-time metadata only** — it records the birth state (`pending`) and
  is deliberately **not** rewritten on transition (that would break the single-`os.link`-CAS
  property, §6.1). The authoritative current state is *which directory the file is in*; derive it via
  `state_of()`, never from the field. (Corrected in slice 2: the earlier "mirror, rewritten on each
  move" wording was spec drift — the directory is the sole source of truth.)

---

## 6. State machine & concurrency

```
pending/ ──claim──▶ claimed/ ──done──▶ done/ ──archive──▶ archive/
```

### 6.1 A no-overwrite hard link *is* the lock
`claim` performs `os.link(pending/<id>.md, claimed/<id>.md)` then `os.unlink` of the source — a
no-overwrite hard link is a true single-winner compare-and-swap: exactly one process creates the
destination, losers get `FileExistsError` and print `already claimed` (exit code 3). No separate
lockfile is needed. **Correction (slice-2 adversarial verification):** the intuitive
`os.replace(src, dst)` is atomic but is **NOT** single-winner on Windows — `MoveFileExW` lets N
concurrent racers on the same `src→dst` all "succeed", so multiple agents would each believe they
exclusively claimed the same handoff (lost mutual exclusion). Verified 10/10 multi-winner with
`os.replace`, exactly-one-winner with the `os.link` CAS. Residual: a crash between `link` and
`unlink` leaves the file linked in both dirs (cleanup debt, not corruption); the next attempt hits
`FileExistsError` → clean lost-race.

### 6.2 Coarse lock for multi-step ops
Operations that aren't a single rename (id allocation, registry writes) take a coarse lock by
**atomic directory create**: `mkdir $collab/logs/.lock`. Held briefly, released in a `finally`.
A stale lock older than `LOCK_TTL` (default 30s) is broken with a logged warning.
**[DECIDED: atomic-mkdir over `flock`]** — `flock` is absent/unreliable on native Windows &
PowerShell; `mkdir` atomicity is universal. The lock lives in the Python core, so both CLI dialects
share identical semantics.

### 6.3 Crash safety & resumability
Every transition is a link+unlink; every state is a directory. A crash always leaves a valid state
(worst case a file linked in two dirs — same content, reconciled on the next attempt). Recovery is
"look at the directories" — no journal to replay, no daemon to restart.

---

## 7. `handoff` CLI — command reference

Invoked as `handoff <collab> <cmd> …`, or as `collab-handoff <cmd> …` when `$HANDOFF_ROOT` is bound
to a collab. Exit codes are stable so watchers/scripts can branch on them.

| Command | Args | Behavior | Exit |
|---|---|---|---|
| `create` | `--to <seat> --title "…" --priority <p> --file <body.md>` | allocate id (§5), render frontmatter + body → `pending/<id>.md`, print the id | `0` ok / `1` bad args |
| `list` | `[--pending \| --claimed \| --done]` | table: id · title · status · age; default = all non-archived | `0` |
| `claim` | `<id>` | atomic `pending→claimed`; print new path | `0` / `3` unclaimable |
| `show` | `<id>` | print the file from whichever state dir holds it | `0` / `4` not found |
| `done` | `<id>` | move `claimed→done` | `0` / `4` |
| `archive` | `<id>` | move `done→archive` | `0` / `4` |
| `status` | — | cross-project overview from the registry: per collab, counts by state + oldest-pending age | `0` |
| `new` | `<name> [--repo <url>] [--reviewer claude\|grok]` | scaffold + register (delegates to `newproject`) | `0` / `1` |
| `register` | `<name> --root <path>` | add/update a registry entry only | `0` / `1` |

Every state-changing command appends an audit line to `$collab/logs/handoff.log`:
`<iso-ts>\t<actor>\t<action>\t<id>`. This log is itself part of the trail the reviewer/human can read.

**[Q, non-blocking]** `archive` cadence: manual (assumed) vs. an automatic sweep of aged `done/`.

---

## 8. Watchers

- **`watch-for-<seat>-handoffs.py`** (claude = builder-side, grok = reviewer-side): poll
  `$HANDOFF_ROOT/handoffs/pending/` every `POLL_SECONDS` (default **2s**, stdlib only — no
  inotify/ReadDirectoryChangesW dependency, which differ per OS). Maintain a persisted seen-set in
  `$collab/logs/watch-<seat>.state` so a restart doesn't re-announce. On a *new* file whose `to:`
  matches this seat, print an in-session banner the agent notices. It also drains
  `inbox/live/<project>/` (Telegram → agent, §9) and surfaces those messages.
- **`watch-all-handoffs.py`** (optional, always-on): iterate every registered collab and fan
  new-handoff events out to the notification channel (Telegram outbox and/or OS notification), so
  nothing waits unseen across projects.

---

## 9. Telegram bridge (`telegram-bridge.py`)

Zero-dependency (`urllib` long-poll) adapter over a **file protocol**. Telegram is optional; the
core loop never imports it.

```
agent → phone:  write  $COLLAB_HOME/outbox/<ts>-<project>.md
                bridge long-polls, sends to the locked chat, and on CONFIRMED delivery
                moves the file to  outbox/archive/
phone → agent:  you send  /c <project> <message>   (Telegram)
                bridge writes  $COLLAB_HOME/inbox/live/<project>/from-user-<ts>.md
                the project's watcher (§8) surfaces it in-session
```

- **Bot:** bring-your-own @BotFather token via `TELEGRAM_BOT_TOKEN`.
- **Chat-id lock:** the first inbound message learns-and-locks the chat id into
  `$COLLAB_HOME/logs/telegram-chat.lock`. For a public bot, set `TELEGRAM_CHAT_ID` explicitly to
  skip learning (and ignore other chats).
- **Path-traversal safety:** `<project>` from `/c` is slug-sanitized (`[a-z0-9-]`) *before* it
  becomes a path; unknown projects are **rejected, not created**.
- **At-least-once delivery:** an outbox file is archived only after Telegram returns `ok`, so a
  crash mid-send re-sends rather than drops.
- **Swap-ability:** because it's only an adapter over `outbox/` + `inbox/`, Slack/Discord/etc. drop
  in the same way.

---

## 10. Adversarial regression hunt (`diff-regression-hunt.workflow.js`)

Runs via the Claude Code `Workflow` tool. **Risk-triggered**: only for diffs touching a project's
declared `guardrails` (`money / safety / data-integrity / auth / concurrency`) — a typo never burns
10–15 agents. Runs **in parallel with** the human-style reviewer: two independent lenses.

```js
Workflow({ scriptPath: "tools/diff-regression-hunt.workflow.js",
           args: { repo, diffPath, guardrails, areas } })
```

```
pipeline over fix-areas (default = git-derived hunks):
  stage 1  probe   → a "breaker" agent tries to break THIS area's change
  stage 2  verify  → each finding → an INDEPENDENT verifier that tries to REFUTE it,
                     defaulting to REJECTED unless it can cite an exact code path + a concrete trigger
→ return { confirmed, refuted }
```

The verify stage is the whole point: it kills the plausible-but-wrong findings that make naive
"ask an LLM to review this" useless. `pipeline()` (not a barrier) so each area's findings verify the
moment that area's probe returns. Each project's `guardrails` are baked into its generated
`PROTOCOL.md` and passed here.

---

## 11. Bootstrapping

### 11.1 `newproject <name> --repo <git-url> --reviewer claude|grok`
1. Create `$COLLAB_HOME/<name>/` with the four handoff dirs, `context/`, `logs/`.
2. Clone `--repo` into place.
3. Render templates — `PROTOCOL.md`, `REVIEWER-BRIEFING.md`, `KICKOFF.md`, `context/IDEA.md` — via
   simple `{{VAR}}` substitution. **Variables:** `{{NAME}} {{REPO}} {{REVIEWER}} {{DATE}}
   {{GUARDRAILS}}`. **Guardrails are written fresh per project — never inherited.**
4. Register in `collabs.json` (atomic write).
5. Print the **session bootstrap block**: the env exports + the two watcher commands (one per seat).

### 11.2 `restart <name>`
Re-derives and re-prints the session bootstrap block for an existing collab (no scaffolding).

### 11.3 `install.sh`
Symlink `bin/*` and `tools/handoff` into `~/bin`, append `COLLAB_HOME` to the shell rc, and — for
Claude Code users — install the `/collab` skill (`skills/collab/SKILL.md`) as the agent front door.

---

## 12. Security model (agent output is untrusted)

- **Path traversal:** every externally-supplied name (`/c <project>`, `--to`, collab names) is
  slug-sanitized to `[a-z0-9-]` before touching the filesystem.
- **No silent creation:** Telegram `/c` to an unknown project is rejected; the bridge never invents
  a collab.
- **Chat-id locking:** the bridge answers exactly one chat.
- **Untrusted diffs:** the regression hunt assumes the diff may be adversarial and defaults its
  verifier to *reject*.
- **Registry integrity:** atomic writes; only two writers.

---

## 13. Build order (each slice = one reviewed handoff)

Per the bootstrap decision (manual handoffs from day one — see `README.md`), collab-kit is built
through its own process. Suggested slice order, smallest-reviewable-first:

1. `lib/collab_common.py` + `.sh` (path resolution, atomic IO, mkdir-lock) — **guardrail: concurrency**.
2. `lib/handoff_core.py` state machine (`create/list/claim/show/done/archive`) + tests.
3. `handoff` / `handoff.ps1` shims; `collab-handoff` wrapper. *(self-hosting handover point: once
   these pass tests, the meta-collab uses them for its own bookkeeping.)*
4. `collabs.json` + `register` + `status`.
5. Watchers.
6. `telegram-bridge.py` — **guardrails: auth (chat-id lock), path-traversal**.
7. `diff-regression-hunt.workflow.js` — **guardrail: safety**; validated by running it by hand first.
8. `newproject` / `restart` / `install.sh` / `/collab` skill.

---

## 14. Open questions (non-blocking)

- `done → archive`: manual (assumed) vs. automatic cadence sweep.
- Whether `watch-all-handoffs.py` should also emit OS-native notifications or Telegram-only.

---

## 15. Canonical mission-control state

The dashboard no longer infers operator meaning from board directories in each renderer. The physical
handoff directory remains the CAS truth, while `tools/lib/operational_state.py` owns effective lifecycle,
append-only per-handoff replay, reconciliation, conflicts, escalation/parked semantics, and action fields.
`dashboard_core.snapshot()` is the one projection used by web and TUI. Web delivery is a resumable,
instance-scoped SSE full-snapshot stream with bounded replay and snapshot reconciliation.

OpenAI-shaped seats are LiteLLM-gateway-only and attach the native Langfuse metadata projection. The proxy
owns generation export; the application retains only redacted per-attempt operational telemetry. See
[`dashboard-operational-state.md`](./dashboard-operational-state.md) for the complete schema, precedence,
health, recovery, privacy, and runbook contract.
