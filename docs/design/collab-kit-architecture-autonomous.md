# collab-kit — Architecture & Build Spec (Autonomous-mode revision)

> Status: **Operating-mode revision** of [`collab-kit-architecture.md`](./collab-kit-architecture.md).
> §1–§14 are copied byte-for-byte from that canonical spec — the numbering is load-bearing (code cites
> it by `§`), so nothing below §14 renumbers it. This revision **appends §15–§21** and rewrites only the
> lede + the approval framing to describe the **fully-autonomous** operating mode. Where this file and the
> original disagree on approval authority, **this file governs**; on everything else the original stands.

collab-kit is a **file-based orchestration layer** for running a *builder + independent-reviewer/verifier*
agent loop over a codebase, with an **optional human/operator channel over Telegram**. It treats agent
output as untrusted code and makes verification a first-class, parallel, independent step. The core loop is
**autonomous**: the safety boundary is not a mandatory human approval gate but **separation of authority** —
*no actor may approve its own work*. The builder implements; an **independent** reviewer/verifier may
advance a handoff to `done/` **iff a machine-checkable evidence contract holds (§18)**; a human remains
available over Telegram as an **override**, never the default bottleneck.

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

# Autonomous-mode addendum (§15–§21)

> Everything below postdates §1–§14. §1–§14 describe the file protocol and the *pre-implementation*
> plan; §15–§21 describe the autopilot driver, the production hardening the build actually required, and
> the **autonomous approval model**. The pipeline in **§10 (adversarial regression hunt) is unchanged and
> load-bearing** — the lane runner (§18.2) is its implementation. Do **not** renumber §1–§14 or §10.

## 15. Autopilot driver (`tools/lib/autopilot.py`)

The driver *closes* the builder↔reviewer loop that §7–§8 only described by hand. It is **agent-agnostic
[C34]**: a seat's backend is a command template — an argv that reads the prompt on **stdin** and returns
text on **stdout** ([C39] backend isolation: `shell=False`, prompt on stdin, output capped at the process
boundary, under a timeout). One mechanism covers headless `claude -p -`, Codex, Grok, Gemini, Cursor;
adding a seat is a `seats.json` entry, not code. A seat with **no** CLI backend is the human/web seat — the
driver leaves its handoffs alone so they go out over the Telegram bridge (§9).

```
pending handoff addressed to seat → claim (link-CAS, §6.1) → CLI backend stdin
    → bounded stdout → _sanitize → autopilot/replies/<ts>-<seat>.md  (inert artifact)
    → create reply handoff whose body is only  AUTOPILOT_REPLY <relpath>
```

Invariants: **bounded** by `max_rounds` ([C35] — the loop can never ping-pong forever; on the cap it writes
a human ping to the outbox); **agent output is untrusted DATA, never control-plane** ([C38] — stdout is
size-capped, de-controlled, and stored as an artifact; the parsed handoff body carries only the pointer, so
an agent cannot forge typed frontmatter/constraints); **backend isolation** ([C39]). Seats live in
`seats.json` at the kit root; keys come from `<kit root>/.env`.

## 16. Production-hardening addendum

Every file-protocol boundary the build crossed had to satisfy the same contract, learned slice by slice
(005/006 adversarial review):

- **Atomic publish** — write `*.tmp`, `os.replace()`; a crash never yields a half-written file.
- **Regular-file checks** — outbox/inbox/pending readers **skip symlink / FIFO / device / oversized**
  entries (never follow a link, never read unbounded).
- **Bounded reads everywhere** — process-boundary stdout/stderr caps on backends (a runaway can't OOM the
  driver); 256 KB caps on artifact/inbox reads.
- **Path-traversal defense** — every externally-supplied name (`/c <project>`, `--to`, ids, pointers) is
  slug/`\d`-validated or resolved through the state machine, **never string-joined into a path**; the
  `AUTOPILOT_REPLY` pointer is constrained to `<collab>/autopilot/replies/` and refuses `../` escapes.
- **Lock-fenced final mutations** — id allocation and registry writes hold the atomic-mkdir coarse lock
  (§6.2); state transitions are the single-winner `os.link` CAS (§6.1); the Telegram bridge holds a
  singleton OS lock.
- **Accepted at-least-once** where exact-once is impossible (outbox archive only after confirmed send;
  inbox drain) — documented, not hidden.

## 17. Verification contract — *source == tested*

A slice is **not** production-grade because a happy-path or mocked test passed, an agent *said* it ran
lanes, docs claim safety, or scratchpad evidence exists. It is production-grade only when the **actual
on-disk source, the tests, the negative fixtures, and the verification ledger (§18.2) agree**.

The load-bearing mechanism is a **source manifest**: `{repo-relative path: sha256}` over the reviewed
files, emitted at evidence-time and **re-verified against the live tree at closeout** by a `gate_runner`
check kind (`source_consistency`, §5.4 substrate). Drift between what was reviewed/tested and what is on
disk is a hard block. This closes the "005 round 3" failure mode, where a reviewer judged a *pasted
snippet* that did not match the tree. Mocked-only coverage is **insufficient** for a process-boundary claim
(caps, timeouts, isolation) — those require the real-subprocess fixtures (§18.2).

## 18. Autonomous approval model

### 18.1 The invariant and the authorities

The safety boundary is **separation of authority**, not a mandatory human gate:

```
No actor may approve its own work.
```

A handoff moves to `done/` only via an **independent approval authority**:

1. **Autonomous reviewer/verifier** — a seat *distinct in seat, prompt, process, and output path* from the
   builder, which reads actual repo state (not builder claims) and emits an explicit approval whose evidence
   contract (§18.3) is satisfied.
2. **Adversarial verification** — the breaker→refute lanes (§18.2) run clean; confirmed blockers fixed;
   ledger clean.
3. **Human override** — a human may approve, block, or re-route over Telegram, but is **not** required on
   the normal autonomous path.

Invalid authorities: the builder approving itself; the driver approving a slice just because `max_rounds`
completed; a test pass without review; a review without source evidence; docs without executable proof.

**Reframed constraints.** [C36] is no longer "the driver never advances a handoff / sign-off stays
human-gated" — it is **"no irreversible step *without a satisfied evidence contract*."** [C38] still holds:
the `[[SIGNOFF]]` token in a reviewer's stdout is **necessary but not sufficient** — the machine-verified
ledger, not the token, advances state. The transition itself is the *same* `os.link`/`done` CAS a human
closeout uses (§6.1), so autonomous and manual closeout are indistinguishable to the state machine.

### 18.2 Adversarial lane runner (`tools/lib/lanes.py`) — the §10 pipeline, implemented

§10's `diff-regression-hunt.workflow.js` was never built (it assumed a Claude-Code `Workflow` dependency,
against the stdlib-only tenet §1). Its shape is re-homed as a stdlib runner over the driver's hardened
backend substrate:

```
per required lane:
  stage 1 breaker  → an agent tries to BREAK this area's change (concrete trigger, not opinion)
  stage 2 verify   → an INDEPENDENT verifier tries to REFUTE each finding, defaulting REJECTED
                     unless it cites an exact code path + a concrete trigger
→ verification ledger  <collab>/autopilot/verification/<hid>.ledger.json
```

Lanes required for a handoff are derived from its `guardrails:` frontmatter (risk class). For the
**autopilot/006 risk class** the five lanes are: **untrusted-agent-output · bounded-autonomy ·
path/pointer-safety · process-isolation · data-integrity-under-concurrent-autopilots.** Independence is
enforced *structurally*: breaker, verifier, and the builder (`from`) must be three distinct seats.

### 18.3 Autonomous Done-Transition Contract

A handoff may be advanced to `done/` autonomously **iff all ten hold**:

1. The builder has produced implementation evidence (source manifest + reply artifact).
2. The approving seat is **not** the same seat/prompt/process/output path as the builder for that decision.
3. The required adversarial lanes ran for the risk class.
4. Every confirmed blocker is fixed.
5. Every blocker has a regression test.
6. Accepted residuals are explicit.
7. Source/test consistency is verified (§17).
8. No stale scratchpad evidence is used (every evidence path resolves under `<collab>/`; the ledger is no
   older than the newest reviewed source).
9. The approval event is recorded in the handoff/trace.
10. The transition uses the same state-machine safety rules as manual closeout (§6).

A `done_contract` evaluator checks all ten and is **pure** (never transitions state); the driver performs
the `done` CAS only on a satisfied verdict. A `[[SIGNOFF]]` token with an *unsatisfied* contract does **not**
advance — it pings the human (override) and the handoff stays `claimed`.

## 19. Roles & seats

The two core roles are **seats**, not people, and are **swappable** (agent-agnostic tenet, §1):

| Role | Live seat (this collab) | Authority |
|---|---|---|
| **Builder** | Claude (`claude -p --model opus`) | implements; **may not** approve its own work |
| **Reviewer / Verifier** | ChatGPT (`gpt-5.6-terra` via the OpenAI-compatible adapter) | independent review; runs/owns the lanes + ledger; may approve **another seat's** work |
| Standby reviewers | Grok, Gemini | act only when a handoff is addressed to them |
| Human | over Telegram | override / block / re-route |

"CLI agents" here **are** LLMs — Claude, ChatGPT (Codex/`openai-compatible-seat.py`), Grok, Gemini,
Cursor — each occupying a seat via the stdin/stdout backend contract (§15). *Reviewer* denotes the
judgement seat; *verifier* denotes the lane/ledger machinery that produces the evidence the judgement rests
on. The pairing is deliberately **cross-vendor** so the reviewer is independent of the builder in model as
well as process.

## 20. Autonomous agent-prompt language & stop conditions

Seat prompts drive the loop without a human by default:

> *You are operating in a fully autonomous collab-kit loop. Do not wait for a human by default. Continue
> until a real stop condition. You may approve only when you are the independent reviewer/verifier for work
> produced by **another** seat and the §18.3 evidence contract holds — you may not approve your own work.*

The reviewer's sign-off token no longer means "looks production-grade"; it **asserts the §18.3 contract
holds**, and the machine, not the token, decides. **Stop conditions** (the loop halts only for a real
blocker): missing credentials; missing repo/file access; a destructive action outside scope; a failed
test/verification lane; a confirmed unresolved blocker; a source/test mismatch; unclear approval authority;
a safety-boundary violation; or the `max_rounds` cap. The loop does **not** stop merely because a human has
not looked yet.

## 21. Updated slice map (reality)

The §13 build order predates the implementation; the handoff ledger records what actually shipped, plus the
autonomy slices this revision adds:

| Slice | Scope | State |
|---|---|---|
| 001 | `collab_common` (path resolution, atomic IO, mkdir-lock) | done |
| 002 | `handoff_core` state machine + id ledger + link-CAS | done |
| 003 | `handoff` CLI + `collabs.json` registry | done |
| 004 | parameterized watchers + persisted seen-state | done |
| 005 | Telegram bridge hardening + inbox drain | claimed (sign-off requested) |
| 006 | autopilot driver (§15) | claimed ("do not sign off yet" — lanes pending) |
| 012 | this autonomous-mode doc | — |
| 013 | `source_consistency` gate kind (§17) | — |
| 014 | `handoff bundle` (evidence packaging) | — |
| 015 | `lanes.py` adversarial-lane runner + ledger (§18.2) | — |
| 016 | `done_contract` + wire into the driver; reframe [C36] docstrings (§18.3) | — |
| 017 | negative-fixture sweep + end-to-end autonomous-done | — |

## 22. Four-role, risk-tiered assurance (ADR-0004)

ADR-0004 supersedes the historical string-lane fan-out described in §10 and §18.2 without changing the
accepted reviewer ∥ lanes lifecycle in §18. The visible topology is exactly four seats: builder (write),
reviewer (read_test), breaker (read_test), and verifier (read_test). read_test means a repository-capable
adapter receives the full configured repository and may run bounded checks; it never means a text-only
reviewer or a write-capable assessor.

For each candidate, verification_plan.py resolves immutable typed contracts from guardrails and config.
There is always one baseline breaker-to-verifier pair; any safety-critical guardrail adds exactly one
provider-diverse composite pair, regardless of how many contracts match. Breakers return at most three
identified findings and one verifier call handles the complete batch. Missing or malformed batch evidence
is verification_incomplete, never a clean lane. The candidate id and immutable ledger carry the resolved
plan/profile/policy fingerprints, so configuration drift cannot reuse old evidence.

The dashboard still has four role cards. Pass/profile badges appear only in lane evidence, and a model
switch is rejected before save if it makes a managed policy invalid or collapses high-risk provider
diversity. See collab/docs/adr/0004-four-role-risk-tiered-assurance.md and collab/SEATS.md for the
normative configuration and failure semantics.

**Renumber hazard (must-read for future edits):** code cites this spec by `§` (e.g. `gate_runner.py`
→ §5.4/§13; `autopilot.py`, `handoff_events.py` → §7.x/§8). This revision is safe **only because it
appends** §15–§22 and preserves §1–§14 and §10. Reflowing the original numbering would break ~37 in-code
citations across 8 library files — do not.
