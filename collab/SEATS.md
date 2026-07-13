# Configuring seats (which agent does what)

Every agent in the loop is a **seat** defined in [`seats.json`](./seats.json) at the kit root. To change
which model/agent fills a role, edit that file and **restart the driver**. There is no hidden agent
selection — this file is the whole control surface.

## The one rule that explains your "reviewer keeps blocking" problem

A seat's `backend` is `"cli"`, and its `cmd` is an argv that **reads the prompt on stdin and writes its
answer on stdout**. There are two very different kinds of `cmd`:

| Kind | Example `cmd` | Can it read your repo? |
|---|---|---|
| **Coding-agent CLI** | `["claude","-p","--model","opus"]` | **YES** — it has file tools, runs in the launch dir, can open files, compute hashes, run tests |
| **API adapter** (a plain chat completion) | `["python", ".../openai-compatible-seat.py", "--model","gpt-5.5", ...]` | **NO** — it's a single LLM call with only the prompt text; no filesystem |

Your **builder** is a coding-agent CLI (`claude -p`), so it can edit code. Your **reviewer** is the API
adapter (`gpt-5.5`) — a single completion with **no repo access**. That is why it replied *"I cannot
verify the builder's claims against the actual repository source"* and refused to sign off every round:
it literally cannot see the files, and the sign-off contract requires `source==tested`. **A reviewer that
must verify source has to be a coding-agent CLI, not an API adapter.**

> Repo-aware seats read files relative to **the directory you launch the driver from**. Launch it from the
> repo whose source is under review (for the collab-kit slices, that's the `collab-kit` folder).

## Billing: Max/Pro subscription vs per-token API

`claude -p` uses **`ANTHROPIC_API_KEY` if it's in the environment** (per-token API billing), and only falls
back to your logged-in **Max/Pro subscription** when that key is absent. The driver loads `.env` into its
own environment and every backend it spawns inherits it — so a `claude -p` seat would bill the **API** by
default here.

To run a Claude seat on the **subscription** instead, add **`"unset_env": ["ANTHROPIC_API_KEY"]`** to that
seat. It drops the key from *that seat's* child process only (other seats keep their keys), and Claude Code
falls back to the subscription. Requires that `claude` is logged into the subscription (`claude` login), and
that `ANTHROPIC_API_KEY` is not also set in your ambient shell/system env (it currently is only in `.env`).

Current setup: only the **`builder`** (Claude) uses `unset_env` -> the subscription. The **`reviewer`,
`breaker`, and `verifier`** are all ChatGPT (OpenAI adapter, per-token `OPENAI_API_KEY`) — the adapter has
no subscription mode. `unset_env` is generic: use it for any provider whose CLI prefers a key over a
subscription. To move the breaker/verifier onto the subscription too, switch their `cmd` to `claude -p`
(different model than the builder) and add `"unset_env": ["ANTHROPIC_API_KEY"]` — that also makes them
repo-aware (see the repo-access rule above).

## Quick recipes (copy a block into `seats.json` → `seats`)

Keys come from `<kit root>/.env`. **Verify the exact flags for the version you have installed** — a wrong
model id or subcommand just 404s / errors to stderr (same caveat as the note already in `seats.json`).

```jsonc
// Reviewer as a repo-aware Claude Code agent (different model than the builder keeps them independent):
"reviewer": {
  "backend": "cli",
  "cmd": ["claude", "-p", "--model", "sonnet"],
  "system": "You are the independent REVIEWER … (keep the [[SIGNOFF]] instructions)",
  "can_sign_off": true,
  "timeout": 900
}

// Reviewer as OpenAI Codex (repo-aware coding CLI):
"reviewer": { "backend": "cli", "cmd": ["codex", "exec", "-"], "can_sign_off": true, "timeout": 900, "system": "…" }

// Reviewer as Gemini CLI (repo-aware):
"reviewer": { "backend": "cli", "cmd": ["gemini", "-p"], "can_sign_off": true, "timeout": 900, "system": "…" }

// Reviewer as the API adapter (CURRENT — fast, but CANNOT verify source, so it will keep blocking sign-off):
"reviewer": {
  "backend": "cli",
  "cmd": ["python", "C:\\Users\\Nalakram\\Documents\\GitHub\\collab-kit\\tools\\adapters\\openai-compatible-seat.py",
          "--base", "https://api.openai.com/v1", "--model", "gpt-5.5", "--key-env", "OPENAI_API_KEY", "--api", "auto"],
  "can_sign_off": true, "timeout": 600, "system": "…"
}
```

The same shapes work for `builder`, `grok`, `gemini` — just change the seat name and `cmd`.

## Fields

- **`backend`** — `"cli"` for an automatable seat; anything else (e.g. `"bridge"`) makes it a human/web
  seat the driver leaves alone (handoffs to it go out over Telegram).
- **`cmd`** — the argv (no shell; prompt on stdin; answer on stdout). Give **only the final answer** on
  stdout — a coding CLI must run in its quiet/print mode or its progress logs pollute the reply.
- **`system`** — prepended to every prompt for that seat. The reviewer/grok prompts carry the `[[SIGNOFF]]`
  instructions; keep them.
- **`can_sign_off`** — `true` lets the seat *assert* the done-contract holds by ending a reply with
  `[[SIGNOFF]]`. It is **necessary but not sufficient**: the machine re-verifies the evidence ledger
  (independent approver, clean lanes, `source==tested`) and refuses the transition if anything is unmet.
- **`unset_env`** — a list of env var names to drop from that seat's backend process (see Billing above).
  `["ANTHROPIC_API_KEY"]` makes a `claude -p` seat use the Max/Pro subscription instead of the API key.
- **`timeout`** — seconds for one backend call. Give coding agents room (600–900s); they're far slower
  than a single API completion.

## Independence

No seat may approve its own work. Keep **builder** and **reviewer** on different seats (ideally different
vendors/models) — the done-contract enforces `reviewer != builder`, and the lane runner requires the
breaker and verifier to be distinct from the builder too.

## Budgets, escalation & reopen (the candidate lifecycle)

The driver is **candidate-based** (see `CONTEXT.md`): it drives a handoff as a sequence of *candidates*,
each **assessed** by the reviewer decision running in parallel with the adversarial lanes, then classified
(`approved` / `repair_required` / `infrastructure_blocked` / `verification_incomplete`). A `repair_required`
candidate sends the worker the exact findings and retries.

Every retry is charged against a named **budget** (work attempts, review decisions per candidate,
verification passes, total model calls, wall-clock — calibrated `balanced()` defaults). When the budget is
exhausted — or a "fix" changes nothing (**no-progress**) — the driver writes a durable **escalation** to
`autopilot/escalations/<hid>.md` (the reproduced defect + the terminal reason) and pauses, awaiting a human.
It never thrashes to a silent stop and never ships on an unsatisfied contract.

- **`--max-rounds`** on the driver CLI is a **deprecated alias** for the work-attempt budget. The dashboard's
  "max turns" control (`control.json`) raises/lowers the same ceiling live.
- The dashboard's **reopen** files a durable operator request (`autopilot/requests/<hid>.json`) the driver
  consumes on its next pass — even if no driver is running now. `retry` re-drives on a fresh, human-authorized
  budget epoch; `adopt` takes the current on-disk source as the candidate. Neither can force a `done` — the
  evidence contract still gates the close.

## After editing

Restart the driver so it reloads `seats.json`:

```
python C:\Users\Nalakram\Documents\GitHub\collab-kit\tools\lib\autopilot.py --collab C:\Users\Nalakram\Documents\GitHub\ibkr-auto-trader --home C:\Users\Nalakram\Documents\GitHub\collab-kit --watch
```
