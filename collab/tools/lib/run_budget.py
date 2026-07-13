"""run_budget — the single deep module that owns bounded-autonomy accounting (ADR-0002).

One `RunBudget` per handoff owns every counter and every limit; the driver charges through it instead of
hand-rolling `thread_rounds`/`total_rounds`/`fix_attempts` inline. The contract (ADR-0002):

- Five named, separately-budgeted things — work attempts, review decisions (per candidate), verification
  passes, model calls, wall-clock — never one overloaded "round" (D1).
- **Atomic reservation, not check-then-charge** (D6): parallel lanes race a *may-I? … then charge*
  sequence, so every charge is a single locked+persisted operation that either succeeds or denies. A
  reservation is spent even if the call it paid for then fails — you cannot farm free retries off a flaky
  backend.
- `max_total_model_calls` and `max_wall_clock` are **global ceilings** every call / all elapsed time draw
  against; the rest are per-kind (D1).
- `max_review_decisions_per_candidate = 1` is a **contract invariant** (D5), not a runtime knob.
- **Reopen = a new, human-authorized epoch** (D6): counters reset for the new epoch; closed epochs stay
  immutable in the record, so "why did this handoff get more budget?" always has an audited answer.

Persisted to `autopilot/budget/<hid>.json` (atomic, `[C16]`). Single-process per handoff — the handoff
claim serialises drivers — so an in-process lock plus atomic disk persist is sufficient.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402

SCHEMA_VERSION = "0.1"

# Charge kinds.
WORK_ATTEMPT = "work_attempt"
REVIEW_DECISION = "review_decision"
VERIFICATION_PASS = "verification_pass"
VERIFICATION_CALL = "verification_call"  # a breaker or a per-finding verifier invocation


@dataclass(frozen=True)
class Limits:
    """The budget ceilings. Numeric defaults are calibration knobs (ADR-0002 Open questions); the
    implementation only fixes `max_review_decisions_per_candidate = 1` as a contract invariant (D5)."""

    max_work_attempts: int
    max_verification_passes: int
    max_total_model_calls: int
    max_wall_clock_seconds: float
    max_findings_per_lane: int
    max_review_decisions_per_candidate: int = 1  # D5 invariant — not control.json-adjustable

    def __post_init__(self) -> None:
        if self.max_review_decisions_per_candidate != 1:
            # D5: raising this is a future ADR, never a config change.
            raise ValueError("max_review_decisions_per_candidate is a contract invariant fixed at 1")

    @classmethod
    def balanced(cls) -> Limits:
        """The calibrated 'balanced' defaults (ADR-0002 Open questions; ADR-0003).

        3 work attempts, 3 verification passes, 16 total model calls, 30 min wall-clock, and
        4 findings verified per lane. These are calibration knobs, not contract — a run may
        override them (and `control.json` may raise them mid-run for the current epoch).
        """
        return cls(
            max_work_attempts=3,
            max_verification_passes=3,
            max_total_model_calls=16,
            max_wall_clock_seconds=1800.0,
            max_findings_per_lane=4,
        )


class BudgetExceeded(cc.CollabError):
    """A charge was denied because it would exceed a budget. Terminal: the driver pauses (ADR-0002 D9)."""

    def __init__(self, which: str, consumed: float, limit: float) -> None:
        super().__init__(f"budget '{which}' exhausted: consumed={consumed} limit={limit}")
        self.which = which
        self.consumed = consumed
        self.limit = limit


def candidate_id(source_manifest: dict, *, source_roots, test_command, lane_config) -> str:
    """A candidate's identity = SHA-256 over the source **and** the verification plan (ADR-0002 D3).

    Binding the plan (source roots + test command + lane/guardrail config) into the id means a tightened
    verification policy mints a *new* candidate and can never reuse evidence gathered under the looser plan.
    """
    payload = {
        "source": source_manifest or {},
        "plan": {
            "source_roots": sorted(source_roots or []),
            "test_command": test_command,
            "lane_config": lane_config or {},
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "cand:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _fresh_epoch(started_ts: str) -> dict:
    return {
        "started_ts": started_ts,
        "work_attempts": 0,
        "actor_turns": 0,
        "verification_passes": 0,
        "verification_calls": 0,
        "total_model_calls": 0,
        "review_decisions": {},  # candidate_id -> count
    }


class RunBudget:
    """Owns one handoff's budget record: current-epoch counters, closed-epoch history, and the limits."""

    def __init__(
        self,
        collab,
        hid: str,
        limits: Limits,
        *,
        wall_clock=time.time,
        now_ts=None,
    ) -> None:
        self._path = Path(collab) / "autopilot" / "budget" / f"{cc.slugify(hid)}.json"
        self._hid = hid
        self._limits = limits
        self._wall_clock = wall_clock
        self._now_ts = now_ts or _iso_now
        self._lock = threading.RLock()
        self._record = self._load_or_create()
        # Epoch wall-clock origin: taken once at construction so a within-process run measures elapsed from
        # when the budget was opened this run (persisted started_ts is for the audit trail / reporting).
        self._epoch_wall_origin = self._wall_clock()

    # ---- properties ------------------------------------------------------- #
    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def epoch(self) -> int:
        return int(self._record["epoch"])

    def consumed(self) -> dict:
        """A copy of the current epoch's counters."""
        with self._lock:
            return dict(self._record["current"])

    # ---- charging (atomic) ------------------------------------------------ #
    def charge(self, kind: str, *, candidate: str | None = None) -> None:
        """Atomically reserve one unit of `kind`, or raise `BudgetExceeded` without modifying state.

        The reservation is spent even if the paid-for call later fails — callers charge *before* dispatch
        and never refund (ADR-0002 D6).
        """
        with self._lock:
            cur = self._record["current"]
            if kind == WORK_ATTEMPT:
                self._guard("work_attempts", cur["work_attempts"], self._limits.max_work_attempts)
                self._guard_total(cur)
                cur["work_attempts"] += 1
                cur["actor_turns"] += 1
                cur["total_model_calls"] += 1
            elif kind == REVIEW_DECISION:
                if candidate is None:
                    raise ValueError("REVIEW_DECISION requires a candidate id")
                seen = int(cur["review_decisions"].get(candidate, 0))
                self._guard(
                    "review_decisions", seen, self._limits.max_review_decisions_per_candidate
                )
                self._guard_total(cur)
                cur["review_decisions"][candidate] = seen + 1
                cur["actor_turns"] += 1
                cur["total_model_calls"] += 1
            elif kind == VERIFICATION_PASS:
                self._guard(
                    "verification_passes",
                    cur["verification_passes"],
                    self._limits.max_verification_passes,
                )
                cur["verification_passes"] += 1
            elif kind == VERIFICATION_CALL:
                self._guard_total(cur)
                cur["verification_calls"] += 1
                cur["total_model_calls"] += 1
            else:
                raise ValueError(f"unknown charge kind: {kind!r}")
            self._persist()

    def check_wall_clock(self) -> None:
        """Raise `BudgetExceeded` if the current epoch has run past `max_wall_clock_seconds`."""
        elapsed = self._elapsed_seconds()
        if elapsed >= self._limits.max_wall_clock_seconds:
            raise BudgetExceeded("wall_clock", round(elapsed, 3), self._limits.max_wall_clock_seconds)

    def cap_lane_findings(self, surfaced: int) -> dict:
        """Split a lane's surfaced findings into the verifiable head and the overflow tail.

        ADR-0002 D7 (fail-closed on overflow): a lane verifies at most `max_findings_per_lane`
        findings; the un-verified excess is recorded explicitly (never silently dropped) and
        forces the pass's verdict to `verification_incomplete`. This is the single place the
        cap is applied, so `max_findings_per_lane` is enforced rather than merely stored.
        """
        cap = int(self._limits.max_findings_per_lane)
        n = max(0, int(surfaced))
        verify = min(n, cap)
        return {"verify": verify, "overflow": n - verify, "cap": cap}

    # ---- terminal reporting ---------------------------------------------- #
    def exhausted(self) -> str | None:
        """The name of a *global* budget at/over its limit (for a proactive pause), or None.

        Per-candidate review decisions are not a global exhaust — they gate re-deciding one candidate, not
        the run — so they are excluded here.
        """
        with self._lock:
            cur = self._record["current"]
            checks = [
                ("work_attempts", cur["work_attempts"], self._limits.max_work_attempts),
                ("verification_passes", cur["verification_passes"], self._limits.max_verification_passes),
                ("total_model_calls", cur["total_model_calls"], self._limits.max_total_model_calls),
            ]
            for name, used, limit in checks:
                if used >= limit:
                    return name
            if self._elapsed_seconds() >= self._limits.max_wall_clock_seconds:
                return "wall_clock"
            return None

    def report(self) -> dict:
        """Per-budget `{consumed, limit}` for the pause artifact (ADR-0002 D9), including the epoch."""
        with self._lock:
            cur = self._record["current"]
            return {
                "epoch": self.epoch,
                "budgets": {
                    "work_attempts": {
                        "consumed": cur["work_attempts"],
                        "limit": self._limits.max_work_attempts,
                    },
                    "verification_passes": {
                        "consumed": cur["verification_passes"],
                        "limit": self._limits.max_verification_passes,
                    },
                    "verification_calls": {"consumed": cur["verification_calls"], "limit": None},
                    "total_model_calls": {
                        "consumed": cur["total_model_calls"],
                        "limit": self._limits.max_total_model_calls,
                    },
                    "actor_turns": {"consumed": cur["actor_turns"], "limit": None},
                    "wall_clock_seconds": {
                        "consumed": round(self._elapsed_seconds(), 3),
                        "limit": self._limits.max_wall_clock_seconds,
                    },
                },
            }

    # ---- reopen ----------------------------------------------------------- #
    def new_epoch(self, *, authorized_by: str) -> int:
        """Close the current epoch (immutably) and open a fresh, human-authorized one (ADR-0002 D6)."""
        with self._lock:
            closed = dict(self._record["current"])
            closed["epoch"] = self.epoch
            closed["closed_ts"] = self._now_ts()
            self._record["epochs"].append(closed)
            self._record["epoch"] = self.epoch + 1
            self._record["authorized_by"] = authorized_by
            started = self._now_ts()
            self._record["current"] = _fresh_epoch(started)
            self._record["current"]["authorized_by"] = authorized_by
            self._epoch_wall_origin = self._wall_clock()
            self._persist()
            return self.epoch

    # ---- internals -------------------------------------------------------- #
    def _elapsed_seconds(self) -> float:
        return max(0.0, float(self._wall_clock()) - float(self._epoch_wall_origin))

    def _guard(self, which: str, used: int, limit: int) -> None:
        if used >= limit:
            raise BudgetExceeded(which, used, limit)

    def _guard_total(self, cur: dict) -> None:
        self._guard("total_model_calls", cur["total_model_calls"], self._limits.max_total_model_calls)

    def _persist(self) -> None:
        self._record["limits"] = asdict(self._limits)
        self._record["updated_ts"] = self._now_ts()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        cc.safe_write(self._path, json.dumps(self._record, indent=2, sort_keys=True) + "\n")

    def _load_or_create(self) -> dict:
        """Load the persisted budget record, or bootstrap a fresh one — **fail closed**.

        Mirrors `registry.load`'s three-case handling so a bad byte cannot silently reset the
        budget (ADR-0002 [C16], ADR-0003):
          * **absent** (`FileNotFoundError`) → a fresh record (normal first use);
          * **transiently locked** (`PermissionError` — a concurrent writer's `os.replace` on
            Windows) → bounded retry, then raise;
          * **corrupt / torn** (invalid JSON) → **raise** `cc.CollabError` rather than
            recreating fresh. A silent reset would zero the consumed counters and hand the run
            a full new allowance with no audit trail — a budget bypass.
        """
        text = None
        for attempt in range(5):
            try:
                text = self._path.read_text("utf-8")
                break
            except FileNotFoundError:
                return self._fresh_record()
            except PermissionError as exc:
                if attempt == 4:
                    raise cc.CollabError(
                        f"could not read budget {self._path} after retries "
                        f"(locked by a concurrent writer?)"
                    ) from exc
                time.sleep(0.02 * (2**attempt))
        try:
            rec = json.loads(text)
        except ValueError:
            raise cc.CollabError(
                f"budget record {self._path} is corrupt (invalid JSON) — refusing to proceed "
                f"and overwrite it; inspect/repair or remove it (a silent reset would be a "
                f"budget bypass)"
            ) from None
        rec.setdefault("epochs", [])
        rec.setdefault("current", _fresh_epoch(self._now_ts()))
        rec.setdefault("epoch", 0)
        return rec

    def _fresh_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "hid": self._hid,
            "epoch": 0,
            "epochs": [],
            "current": _fresh_epoch(self._now_ts()),
        }


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
