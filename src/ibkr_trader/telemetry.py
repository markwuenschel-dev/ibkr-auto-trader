"""telemetry — the §8 append-only event record (Reusable Core §8 / §13 step 1: "log first").

Everything the risk-bounded core will eventually learn from — the router, the risk model, the judge
calibrator (§11) — is trained on these traces, so the envelope is fixed *now*, before any of that
exists. Each event is one self-describing JSON object appended to a JSONL run log; the `risk`, `eval`,
and `gates` blocks are present-but-null in the fail-closed bootstrap and fill in as those layers land.

This is the observability that makes an autonomous run *legible* (the thing that was missing when the
loop "went out of order and couldn't be tracked"). stdlib only — json, no structlog dependency yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = "0.1"

# The decision actions the control plane may record (Reusable Core §8 envelope).
ACTIONS = ("accept", "revise", "reject", "escalate", "skip", "waive")


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def new_span_id() -> str:
    return f"sp-{uuid.uuid4().hex[:12]}"


def _content_hash(payload: dict) -> str:
    pre = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()


@dataclass
class Emitter:
    """Writes §8 JSONL events for one run. Append-only; every event carries a content hash so the log
    doubles as an immutable audit record."""

    run_id: str = field(default_factory=new_run_id)
    trace_id: str = field(default_factory=lambda: f"tr-{uuid.uuid4().hex[:12]}")
    log_path: Path = field(default_factory=lambda: Path("logs") / "telemetry.jsonl")

    def emit(
        self,
        *,
        stage: str,
        agent_role: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        linked_span_ids: list[str] | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
        artifact_version: str | None = None,
        input_contract_hash: str | None = None,
        output_contract_hash: str | None = None,
        decision: dict | None = None,
        metrics: dict | None = None,
        gates: list[dict] | None = None,
        eval: dict | None = None,
        risk: dict | None = None,
        failure: dict | None = None,
    ) -> dict:
        """Build, hash, append, and return one §8 event. Best-effort write: a telemetry failure must
        never break the loop (same rule as collab-kit's `_emit_safe`)."""
        event = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "span_id": span_id or new_span_id(),
            "parent_span_id": parent_span_id,
            "linked_span_ids": linked_span_ids or [],
            "run_id": self.run_id,
            "task_id": task_id,
            "agent_role": agent_role,
            "stage": stage,
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
            "input_contract_hash": input_contract_hash,
            "output_contract_hash": output_contract_hash,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "decision": decision,  # {action, reason_codes[], confidence}
            "metrics": metrics or {},  # {latency_ms, *_tokens, tool_calls, cost_usd}
            "gates": gates or [],  # [{name, status, ruleset_hash, severity}]
            "eval": eval,  # {judge_mode, panel, score, ci_*, gold_split}
            "risk": risk,  # {p_error, ucb, tau, waived, audit_*} — null now
            "failure": failure,  # {class, severity, escaped}
        }
        if decision is not None and decision.get("action") not in ACTIONS:
            raise ValueError(f"decision.action must be one of {ACTIONS}, got {decision.get('action')!r}")
        event["event_id"] = _content_hash({k: v for k, v in event.items() if k != "ts"})
        self._append(event)
        return event

    def _append(self, event: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # durable audit record
        except OSError as e:  # observability must never break the loop
            print(f"[telemetry] append failed (continuing): {e}")
