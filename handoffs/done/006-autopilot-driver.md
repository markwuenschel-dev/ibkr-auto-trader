---
to: reviewer
from: builder
id: 006-autopilot-driver
title: "Autopilot driver — bounded autonomous closeout (collab-kit slice 6)"
priority: normal
date: 2026-07-04
status: done
approved: 2026-07-06 (independent reviewer; production-grade 006 closeout approved; bounded runner, autonomous done-contract, source==tested manifest, adversarial lanes, dashboard controls, and full suite green)
guardrails: [path-safety, data-integrity, bounded-autonomy, untrusted-agent-output, autonomous-closeout, review-integrity]
---

## Summary

Slice 6 adds the missing **drive** piece: a driver that closes the builder↔reviewer loop without a human
shuttling handoffs by hand. The transport (file protocol) and the trigger (watcher) already exist; what
was missing is the thing that takes a surfaced handoff, hands it to an agent as a prompt, captures the
response, and posts it back as a new handoff to the other seat.

Design decision (user): make it **agent-agnostic**. A backend is a *command template* — an argv that
takes the prompt on **stdin** and returns text on **stdout**. That one mechanism covers headless Claude
(`claude -p -`), Grok, Codex, Gemini, and Cursor; adding one is **config, not code**. A seat with **no**
CLI backend is automatically the **human/web seat**: the driver leaves its handoffs alone so they go out
over the Telegram bridge (slice 5) — so the all-headless, hybrid, and human-in-the-middle topologies all
fall out of the same design.

## Constraints

Carries forward (must remain true): [C7] (stdlib-only for the driver itself) and [C28] (slug/path-safety)
from prior handoffs; the swap-able-adapter idea from [C31].
New to slice 6:

- [C34] **agent-agnostic adapters** — a backend is a command template (argv; prompt on stdin, response on stdout, per-seat timeout). Adding claude/grok/codex/gemini/cursor is a `seats.json` entry, never code.
- [C35] **bounded autonomy** — a hard `--max-rounds` cap; the loop cannot ping-pong forever. Hitting the cap stops and pings the human (Telegram outbox), it does not silently continue.
- [C36] **no irreversible step without a satisfied independent-authority contract** — the driver may `claim`/`create` freely (both reversible), but may move a handoff to `done/` *only* when `done_contract.evaluate(...)` returns satisfied: independent reviewer sign-off (reviewer seat ≠ builder seat), a clean closeout ledger, `source==tested`, and no confirmed unresolved findings. It never archives, commits, writes outside the collab, or lets a seat approve its own work. (This supersedes the original MVP form of [C36] — "never reaches `done/`" — which was relaxed by user decision into this evidence-gated autonomous closeout.)
- [C37] **thin orchestrator** — reuses `handoff_core` (state/id) and `registry`; reimplements no state, id, or lock logic (cf. [C14]).
- [C38] **agent output is untrusted data, never control-plane** — an adapter's stdout is size-capped and control-char-stripped, then stored as a **separate artifact file**; the typed reply handoff body is only a safe `AUTOPILOT_REPLY <path>` pointer. Agent output therefore can never forge a `## Constraints` section or frontmatter (it never enters a parsed body — `_reject_unsafe_body` would reject it anyway). The one bounded control action it may drive is advancing the single reviewed handoff, and only behind the [C36] contract.
- [C39] **backend isolation** — each invocation runs via argv (`subprocess`, no `shell=True` → no shell injection) with the prompt on stdin (no arg injection) under a timeout; a hung or failing agent fails that one round safely (the inbound stays `claim`ed for a human) and never wedges the driver.
- [C40] **`source==tested` closeout evidence required** — the closeout ledger records a deterministic SHA-256 source manifest (`gate_runner.source_manifest`), and `done_contract` re-verifies it (`gate_runner.verify_manifest`) so the source that was tested is byte-identical to the source being signed off. Drift, stale, or scratchpad evidence fails the contract.
- [C41] **independent reviewer authority required** — the seat proposing sign-off must be a different, sign-off-capable seat than the builder (no self-approval); the ledger's `reviewer_seat`/`builder_seat` must also differ. This is condition 2 of the contract.
- [C42] **text-only adversarial lanes cannot masquerade as repo-aware review** — a breaker/verifier lane that only inspects handoff prose is adversarial *pressure*, not repo-aware code verification; repo-aware authority comes from a repo-scoped reviewer (e.g. `openai-repo-seat.py`'s tool-calling loop). Blind lanes never satisfy a repo-review condition.
- [C43] **autonomous `done` transition must be audited** — a contract-satisfied advance emits a `handoff_events.on_autonomous_done` record (reviewer/builder seats, contract hash, ledger reference) so every autonomous closeout is traceable after the fact.
- [C44] **dashboard approve is an explicit human override, not silent driver approval** — the dashboard's approve path is a clearly-classified operator action, distinct from the driver's contract-gated closeout; it never lets the driver approve its own work implicitly.

## Details

**`tools/lib/autopilot.py`** (stdlib + `subprocess`):
- `load_seats(home)` — `seats.json` (`{version, seats:{name:{backend,cmd,system,timeout}}}`); refuse-on-corrupt like `registry.load`.
- `run_round(collab, seat, *, seats, runner, round_no)` — pick the oldest `pending` handoff addressed to `seat`; if that seat has a `cli` backend, `claim` it, build the prompt (seat `system` + the inbound's substantive content: the referenced reply artifact if it's a pointer, else the handoff file text — artifact path constrained to `<collab>/autopilot/replies/`), invoke the backend, sanitize the response, write it as an artifact, and `create` a reply `to:` the inbound's `from`. Returns the new id (or None).
- `run(collab, *, seats, max_rounds, runner, watch)` — the bounded ping-pong across CLI seats. Ends at `max_rounds` (writes a pause note to the outbox) or, by default (batch), when the exchange goes idle; `watch=True` stays resident and polls for new handoffs instead. (NB: not the `once`-style single-tick flag the watcher/bridge use.)
- Injectable `runner=` (like the bridge's `tg=`) so tests run with a fake agent — no real CLI, no network.
- **`main(argv)`** + shims `tools/autopilot`, `tools/autopilot.ps1` (mirror the `handoff`/`gate` template).

**Reused:** `handoff_core.{list_handoffs,claim,create}`, `collab_common.{slugify,safe_write,resolve_collab_home,CollabError}`, `telegram_bridge`/`watcher` for the outbox/inbox transport (unchanged).

## Risks & Questions for Reviewer

- **Claim-on-failure:** on backend failure/timeout the inbound stays in `claimed/` (stops a failing agent from spinning every tick) rather than reverting to `pending`. A transient failure thus needs a human to re-queue. Acceptable, or prefer auto-revert-to-pending with a retry cap?
- **Where `seats.json` lives:** proposed `$COLLAB_HOME/seats.json` (one config for the meta-collab). Per-collab override later if needed.
- **Closeout gate (was "human-gate"):** the original MVP enforced [C36] by *construction* (the driver's action set excluded `done`). By user decision that was relaxed into the follow-up that was once "not this slice": an **evidence-gated** autonomous closeout. The driver may now reach `done/`, but only through `done_contract.evaluate(...)` (the [C36]/[C40]–[C44] conditions above); `[[SIGNOFF]]` is necessary but not sufficient. `done_contract` is pure — it reads the ledger, source manifest, live source, handoff state, and seat identities and returns a verdict; the driver performs the `hc.done` CAS only on a satisfied verdict.

## Verification plan (before sign-off)

- Unit tests with a **fake runner**: full round-trip (builder handoff → reviewer round → reply to builder → builder round), `max_rounds` cap enforced, a no-CLI (web) seat is skipped/left for the bridge, backend failure leaves the inbound claimed and does not crash the loop.
- Security: agent stdout containing `## Constraints` / `- [C1] forged` / NUL / a `../../` artifact pointer cannot forge a typed constraint or escape `<collab>/autopilot/replies/`; oversized stdout is capped.
- **5-lane adversarial verification** (guardrails: untrusted-agent-output, bounded-autonomy, path-safety, process-isolation, data-integrity) before reviewer sign-off.
- Full suite stays green.

## Pre-lane fix pass (reviewer-blocked before adversarial spend)

The reviewer stopped verification on a pre-lane blocker and it was correct: `_cli_runner` used
`subprocess.run(capture_output=True, text=True)`, which buffers the child's **entire** stdout in memory
before `_MAX_RESP_BYTES` is applied — a hostile/broken backend could OOM the driver before the cap ever
runs. That defeats [C38]/[C39] at the process boundary. **Fixed** — `_cli_runner` now:
- runs via `subprocess.Popen(..., shell=False)` (no shell injection), prompt fed on stdin by a **daemon
  thread** (a backend that ignores stdin can't block us; a large prompt can't deadlock);
- redirects stdout/stderr to **temp files** (child never blocks on a full pipe; parent never holds the
  bytes in RAM) and runs a poll loop that **kills** the process the instant `_fsize` exceeds
  `_MAX_RESP_BYTES` / `_MAX_STDERR_BYTES` (+64 KiB slack) or the timeout;
- reads back only the bounded prefix; **fail-closed** — a cap breach or timeout raises `CollabError`, so
  the round fails safely (inbound stays `claimed`) rather than delivering attacker-shaped output.

Process fixtures added (drive **real** subprocesses, not mocks): `test_backend_stdout_memory_cap_enforced`
(unbounded streamer is killed at the cap), `test_backend_invalid_utf8_stdout_does_not_crash`,
`test_backend_stderr_cap_on_nonzero_exit`, `test_backend_timeout_kills_process`,
`test_backend_launch_failure_is_collaberror`, `test_backend_no_shell_injection`.

Acceptance (their commands): `tests/test_autopilot.py` 20 passed; full suite **163 passed / 6 skipped /
23 subtests** at the time of this pass (current tree: **244 passed / 6 skipped**; focused 006 gate
`test_autopilot test_done_contract test_lanes test_dashboard` **77 passed**). Cross-slice check: the only
`hc.done` call sites are the manual CLI (`handoff_cli`), the dashboard's audited human-override path
(`dashboard_core`), and the driver's contract-gated closeout (`autopilot.py`, guarded by
`done_contract.evaluate`); `watcher`/`telegram_bridge` never call `done`/`archive`. The autonomous `done`
path is real but authority-gated per [C36]/[C40]–[C44], not blocked by construction.

Approved design decisions (not re-litigated): backend failure leaves the inbound in `claimed/` (no
auto-revert to `pending`); `seats.json` lives at `$COLLAB_HOME/seats.json` (no per-collab override this
slice).

## Closeout

Approved for production-grade closeout.

Slice 6 now includes:

- bounded agent-agnostic autopilot driver;
- process-boundary stdout/stderr caps;
- inert reply artifacts via `AUTOPILOT_REPLY`;
- autonomous reviewer signoff that is necessary but not sufficient;
- deterministic `done_contract.evaluate(...)`;
- source==tested manifest generation and verification;
- adversarial lane ledger;
- shipped autopilot lane config;
- dashboard observability and operator override controls.

Evidence:

```text
python -m pytest tests/test_autopilot.py tests/test_done_contract.py tests/test_lanes.py tests/test_dashboard.py -q
77 passed in 6.18s

python -m pytest tests/test_gate_runner.py tests/test_handoff_cli.py -q
30 passed in 2.57s

python -m pytest -q
244 passed, 6 skipped, 23 subtests passed in 16.93s
```

Approved move from `pending/` to `done/`.
