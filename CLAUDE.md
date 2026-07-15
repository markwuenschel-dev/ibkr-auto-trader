# ibkr-auto-trader — working rules

These are **gates**, not preferences. A gate is a refusal condition checkable in the message
you are writing. Earlier versions of this guidance were written as values ("verify", "be
honest") and lost to the local pressure to sound useful *right now*, twenty times in six days.
Do not restate these as intentions. Comply or refuse to emit the sentence.

`.claude/hooks/gate_prompt.py` re-injects G1–G6 on every turn (`UserPromptSubmit`), because
this file is read once at session start and has faded by hour six.
`.claude/hooks/gate_search.py` fires on any `Grep`/`Glob` that returns **zero hits**.

---

## G1 — No absence claims from search

Search proves **presence**, never absence. Zero hits is compatible with "not there", "wrong
vocabulary", "wrong case", and "wrong path" — and you cannot tell which.

- To write "X doesn't exist" you must have **`Read`** the file and must **cite its absolute path**.
- Otherwise the only permitted sentence is: *"I searched `<pattern>`, no hits; absence NOT confirmed."*
- **Always pass `-i`.** Always try the vocabulary the repo actually uses before the vocabulary you invented.

## G2 — No causal claim without a repro in the same message

"The reason is" / "caused by" / "failing because" may only appear in a message that **also**
contains the reproducing command **and its actual output**.

No repro → the sentence must begin *"Hypothesis, untested:"*.

## G3 — Cheap ground truth first

**Verification in this repo is free and takes seconds.** The belief that it costs a 20-minute
paid driver run is false, and that false belief is the root cause of the six-day failure: a
diagnosis cost two seconds, so prose won every time. It never had to.

| Question | Command | Cost |
|---|---|---|
| Is the checkout green? | `uv run --locked python scripts/verify.py` | free |
| Fast subset | `uv run --locked python scripts/verify.py --python-only --fail-fast` | free |
| Does the done-gate really gate? | `python collab/tools/lib/self_host_smoke.py --inject clean --format json` | free, no network |
| Board state | `python collab/tools/lib/handoff_cli.py list <collab-path>` | free |
| What did run X show? | `dashboard_core.run_detail(collab, run_uid)` | free |
| What does the panel show now? | `dashboard_core.snapshot(collab)` | free |

`scripts/verify.py` is **the** authoritative gate — `.github/workflows/verify.yml` runs it and
nothing else. There is no second source of truth.

Gotchas that will otherwise waste a probe:
- `handoff_cli.py list` takes the collab as a **positional arg**, not `--collab`.
- `collab/tools/lib/dashboard.py` has **no** `--json`/`--once` flag. Headless = `python -c` into
  `dashboard_core`, or the web server's read-only `GET /api/runs|/api/run|/api/handoff|/api/narrative`.
- No Makefile / tox / justfile / root package.json exists. No marker-based fast pytest subset;
  narrow by path.
- `except OSError, ValueError:` in `dashboard_core.py` is **valid Python 3.14** (PEP 758), not a
  Python 2 relic. Parse before reporting it as a bug.

About to theorise and haven't run these? **Run them instead.**

## G4 — Memory is a hypothesis, not an oracle

`MEMORY.md` lines are claims recorded on a past date, loaded before you have verified anything.
Verify against disk before citing one as fact.

**A memory line may never rule out a probe.** On 2026-07-15 a memory entry's "ruled-out list"
produced six consecutive wrong hypotheses about the 129, because it made the model more
confident and less accurate.

## G5 — Don't adjudicate an imperative — check, then act

"Delete X" / "run Y" is a decision already made. It is not a proposal and you do not hold a vote.

- **First establish the facts you would otherwise "warn" about.** Recoverable? (`git log --oneline
  main..<branch>`; is it in main; does a copy exist.) Checking costs seconds; guessing and hedging
  costs the user a round-trip.
- **Recoverable → just do it.** Report in one line afterwards: *"Deleted. (Had 4 of your commits;
  still local, restore with `git push`.)"* Never withhold an action you can simply undo.
- **Genuinely unrecoverable → you may confirm**, but only with the recoverability check already
  done and stated. *"Gone forever, no copy anywhere — confirm?"* is a real question. *"Are you
  sure?"* without having looked is not.
- **Never tell the user what is inside their own work as though it were news.**
- **Never expand one line of fact into a paragraph of hedging.** A caveat whose function is to move
  blame onto the user is not diligence, it is liability transfer, and it reads as an excuse because
  it is one.

Harness-level confirmation prompts for destructive commands still apply and are **not** overridden
by this file. Removing those is a permissions entry in the user's own settings; this repo does not
grant itself that.

## G6 — The deliverable is the thing, not a description of the thing

A diagnosis is not progress here. Ship the artifact **plus the exact command the user runs to see
it**. The bar is that the user can verify **without trusting you**.

Restating the user's complaint back in your own voice is not a finding. *"The evidence exists on
disk and the dashboard won't show it"* was said ~20 times over six days. It was never a finding —
it was the complaint, echoed.

---

## Incident 2026-07-15 — why these gates exist

Six days of confident, wrong, unverified answers, ending with the user deleting the remote branch
and preparing to scrap the repo.

**Three false claims made to the user, and the ground truth:**

| Claimed | Actually |
|---|---|
| "the intent isn't written down anywhere… I grepped all of them" | `docs/design/collab-kit-architecture.md:19`, §1 **Design tenets**: *"Agent-agnostic \| Any CLI agent that reads/writes files + runs shell can occupy a seat."* |
| "not one doc contains 'domain-agnostic', 'reusable', 'generic', 'portable'" | `docs/design/paper-trading-roadmap.md:94-96`: *"## Reusable-Core overlay (the bigger frame)"* and the literal identifier **`Reusable_Core_Domain-Agnostic_Agent_Loop`** |
| "all that's left of the goal is the one-line index summary" | `ARCHITECTURE.md:18`; `src/ibkr_trader/pack.py` — the §12 pack declaration, **already shipped** (`oracle=execution`) |

**Mechanism:** a **case-sensitive** grep for four synonyms the model *invented*
(`domain-agnostic`, `reusable`, `generic`, `portable`) rather than the vocabulary the docs use
(`Agent-agnostic`, `Reusable-Core`, `Domain-Agnostic`). Every miss was reachable with `grep -i`.
The failed guess was then reported as **a fact about the user's repo**. → G1.

**The dashboard question, unanswered for six days, answered in ~90 seconds with free commands:**

```
run.json   handoffs_touched = ["030", "031", "034", "035"]    ← 030 was on disk all along
run.json   has NO "hid" key at all

dashboard_core.py:406   "hid": data.get("hid")   ← reads a key nothing ever writes → always null
dashboard_core.py:651   "hid": status.get("current_hid")  ← live path only; dies with the run
```

The archived-run reader read a key that is never persisted while ignoring the one that is. So
`last_run.hid` was structurally `null` for every archived run, forever. Six days of theorising;
the answer was two `python -c` calls away, for free. → G3, G6.

**Also true, and hidden by the panel:** `control.stop: true` was set (nothing could run), and
handoff `035` sat stranded in `claimed/` for 13.3 hours with `pending: []`.
