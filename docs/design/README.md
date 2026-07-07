# Design — collab-kit + ibkr-auto-trader (one unified design)

> Status: **Design (pre-implementation).** These docs fully flesh out the design before any code is
> written. Implementation is a separate, later, reviewed step.

Two layers, designed together around one bet: **agent-generated trading code is untrusted, so
verification must be first-class, parallel, and independent.**

| Doc | What it specifies |
|---|---|
| [`collab-kit-architecture.md`](./collab-kit-architecture.md) | The orchestration tooling: handoff CLI, state machine, watchers, Telegram bridge, regression-hunt, bootstrapping. |
| [`trading-system-design.md`](./trading-system-design.md) | The trading system inside a collab: deep Risk & Sizing + Execution Control modules, domain types, Rules Ledger, strategy seam, invariant→test map. |
| this file | How the two interlock: the two-gate model, the end-to-end lifecycle, and the bootstrap process. |

Source material: [`../../CONTEXT.md`](../../CONTEXT.md) (domain model), [`../../PROTOCOL.md`](../../PROTOCOL.md)
(safety rules), [`../../AGENT-INSTRUCTIONS.md`](../../AGENT-INSTRUCTIONS.md) (roles),
[`../../handoffs/superseded/001-initial-design.md`](../../handoffs/superseded/001-initial-design.md).

---

## 1. The two-gate model

Defense in depth via **two independent gates with different jobs**:

- **collab-kit gates what code *merges*.** builder → independent reviewer **+** adversarial
  regression hunt (in parallel) → human veto → merge. Nothing merges on a single agent's
  self-certification.
- **The trading system's seams gate what can ever *execute*.** Strategy → Risk & Sizing → Execution
  Control → adapter, enforced by *types* and the *Rules Ledger* regardless of who wrote the code.

Why both: the first gate can still let a subtle mistake through; the second guarantees that even a
merged mistake cannot place a live order without passing the mode/config/rules seams. A failure of
one gate is caught or contained by the other.

---

## 2. End-to-end lifecycle

```
1. newproject ibkr-auto-trader --repo <url> --reviewer grok
      → scaffolds the collab; guardrails [money, safety, data-integrity] written into PROTOCOL.md
2. builder implements a slice → handoff create --to reviewer
      → builder-side watcher idle; reviewer-side watcher pings
3. reviewer claims (atomic move) and reads the ACTUAL diff
      → because the diff touches money/safety guardrails, diff-regression-hunt runs IN PARALLEL:
        probe (break it) → verify (refute it) → { confirmed, refuted }
4. human (Mark) sees a Telegram summary; can veto or reply  /c ibkr-auto-trader <message>
5. only when reviewer + regression-hunt + human agree → slice moves to done/ and merges
      → any execution-touching slice can still only reach a broker through the PAPER-default
        Execution Control seam: a merged mistake cannot go live without the mode/config gates
```

The trading system's **paper-first** default (PROTOCOL) is the interlock point: the collab process
decides *what merges*; Execution Control decides *what runs*, and it runs in paper unless live is
explicitly enabled by reviewed config **and** approved workflow state.

---

## 3. Bootstrap process **[DECIDED: manual handoffs from day one]**

collab-kit is built through its **own** process from the first diff — even though the CLI doesn't
exist yet. Process purity over convenience; the first real exercise of the builder/reviewer loop is
collab-kit's own construction (dogfooding by necessity).

1. Hand-create a **meta-collab** for `collab-kit` itself:
   `$COLLAB_HOME/collab-kit/handoffs/{pending,claimed,done,archive}/` + templates, by hand.
2. Each collab-kit slice (see the build order in `collab-kit-architecture.md` §13) is proposed as a
   **hand-written handoff `.md`**; state transitions are **manual file moves** (`mv` / `Move-Item`).
3. An **independent reviewer session** reads the actual diff of every slice. Money/safety/auth-
   adjacent slices (the regression-hunt workflow, the Telegram chat-id lock, the locking core)
   additionally get the adversarial hunt run **by hand** via the `Workflow` tool before the workflow
   file is trusted to run itself.
4. **Self-hosting handover:** once `handoff claim`/`done` pass their tests (slice 3), the meta-collab
   switches from manual file moves to using the CLI for its own bookkeeping.
5. Human veto stays in the loop throughout.

---

## 4. Reconciliation with existing files

The hand-made `PROTOCOL.md`, `REVIEWER-BRIEFING.md`, `KICKOFF.md`, `CONTEXT.md`, and
`handoffs/superseded/001-initial-design.md` are effectively the *rendered templates* of the
`ibkr-auto-trader` collab. collab-kit's `newproject` templates must be a **superset** of what's
already here, so re-bootstrapping this collab would never lose the existing domain model. In
practice: when `newproject` is built (slice 8), diff its rendered output against these files and
fold any gaps back into the templates.

---

## 5. Decisions locked · open questions

**Locked this pass**

- Bootstrap: manual handoffs from day one (meta-collab).
- collab-kit scope: full vision — nothing deferred.
- Runtime: native PowerShell **and** Git Bash, over a shared stdlib-Python core.
- Strategy: rebalancer first, ML signal seam left open.
- Stack: Python 3.14 + uv + ib_async + pydantic (the `pnpm` in handoff 001 is dropped).

**Open (non-blocking — settle during implementation)**

- State store: SQLite (assumed) vs. flat files for P&L / idempotency.
- `done → archive`: manual (assumed) vs. automatic cadence.
- Partial fills / small-account gotchas: model now vs. record as known risks.

---

## 6. Design completeness checklist (verification of the *design*)

- [x] Every collab-kit CLI command has defined args, behavior, exit code (arch §7).
- [x] Every core object in `CONTEXT.md` maps to exactly one model (trading §2).
- [x] Every safety invariant maps to ≥1 planned test (trading §7).
- [x] One full lifecycle traces end-to-end with no undefined step (§2 above).
- [x] All locked decisions recorded; open questions consciously deferred (§5).
