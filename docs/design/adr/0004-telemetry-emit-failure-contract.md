# ADR 0004 — Telemetry `emit()` failure contract: best-effort sink, fail-fast envelope

- **Status:** Accepted — decided 2026-07-18 (integrity-audit flywheel, INT-036). Implemented in
  `src/ibkr_trader/telemetry.py` + `tests/test_bootstrap.py` the same day.
- **Slice:** cross-cutting §8 telemetry seam (PT-0 `Emitter`). Forward-links PT-7 durable decision
  audit (handoff 037), which will introduce the typed `Decision` record this ADR pre-types.
- **Companion:** `CONTEXT.md` (Safety Invariants / observability), `trading-system-design.md` §8,
  `docs/design/codebase-integrity-audit-2026-07-18.html` (INT-036).

## Context

The §8 `Emitter` docstrings advertised a single guarantee — *"a telemetry failure must never break
the loop"* — while `emit()` **raises** `ValueError` on an unknown `decision.action`
(`telemetry.py`, the check next to the `ACTIONS` tuple). The audit (INT-036) read the two as a
contradiction: a documented never-raise guarantee with a real raise underneath it.

Two facts reframed it:

1. The raise is currently **unreachable** in production — the only caller passing a `decision`
   (`app.py` bootstrap) uses a literal `"accept"`; the gateway and assembler `_emit` wrappers pass no
   `decision` and additionally swallow every exception. Verified this run.
2. A test already pins the raise as intended behavior: `tests/test_bootstrap.py`
   `test_invalid_decision_action_rejected`. The *tested intent* was fail-fast; only the docstring was
   wrong.

The real distinction is by **failure class**, not by "does it break the loop":

- A **sink / I/O failure** (disk full, lock contention, missing directory) is a runtime condition
  telemetry must absorb — and `_append` already does, catching `OSError`, logging, and continuing.
- A **malformed envelope** (an `action` outside the closed six-value enum) is a programmer error.
  Absorbing it would write a corrupt event into the append-only, content-hashed JSONL that §11's
  calibrator and the PT-7 durable audit will treat as ground truth. Fail-fast is the only posture
  consistent with an audit log.

## Decision

**Best-effort applies to the sink; the envelope fails fast.**

1. **Keep the `ValueError`** on an unknown `decision.action`. It fails loudly in dev/test rather than
   corrupting the audit record.
2. **Narrow both docstrings** (`TelemetrySink` protocol and `Emitter.emit`) to say exactly that: a
   sink/I/O failure never raises; a malformed envelope is a caller bug and raises. The protocol note
   records the structural reason the gateway/assembler were never exposed to the raise — the protocol
   does not expose `decision` at all.
3. **Type the action (option D).** Declare `DecisionAction = Literal[…]` alongside the runtime
   `ACTIONS: tuple[DecisionAction, ...]` so the static type and the runtime check cannot drift. When
   the PT-7 `Decision` TypedDict/dataclass lands, `action: DecisionAction` slots in and Pyright
   catches a typo at type-check time — closing the one legitimate worry (a future dynamic caller
   typo-ing an action at runtime) at zero runtime cost. The runtime check stays as the belt for the
   dict-shaped callers this schema-light stage still allows.

## Consequences

- The documented guarantee is now true for every caller, not just the suppress-wrapped ones.
- A future hot-path caller that wants blanket immunity wraps `emit` (as gateway/assembler already do);
  the default is loud validation.
- New regression coverage pins **both** halves of the contract: `test_invalid_decision_action_rejected`
  (envelope raises) and `test_sink_failure_never_raises` (sink I/O is swallowed) — the latter was
  previously untested.
- Rejected — **coerce a bad action into a recorded `failure` block** (never raise): it keeps the loop
  alive on a caller bug but writes a corrupt event into the audit log the calibrator trusts, and it
  would invert an existing test. Loop immunity on a *sink* failure already exists; extending it to a
  *malformed envelope* trades correctness for a guarantee no reachable caller needs.
