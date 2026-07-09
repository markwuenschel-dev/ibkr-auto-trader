"""trace.py — append-only JSONL telemetry emitter (ARCHITECTURE.md §8).

Appends one event per line under a ``collab_lock`` so concurrent writers can never interleave a
JSONL line. Reuses the slice-1 substrate (``collab_common``): the atomic/locking primitives we
built and adversarially verified are exactly what make this audit log crash-safe and inspectable.

Usage:
    from trace import emit
    emit(log, run_id="slice-02-handoff-core", stage="build", role="builder",
         artifact="handoff_core.py", decision={"action": "produce", "reason_codes": []})
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc  # noqa: E402

SCHEMA_VERSION = "0.1"


def emit(
    log_path: str,
    *,
    run_id: str,
    stage: str,
    role: str,
    artifact: str | None = None,
    artifact_version: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    decision: dict | None = None,
    ts: str | None = None,
    **extra,
) -> dict:
    """Append one envelope event to ``log_path`` (crash-safe, single-line-atomic)."""
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "stage": stage,
        "role": role,
        "artifact": artifact,
        "artifact_version": artifact_version,
        "decision": decision,
        **extra,
    }
    line = json.dumps(event, separators=(",", ":")) + "\n"
    # Fence the append (approval-bar invariant): only write while we hold the lock's token.
    lockdir = log.with_name(log.name + ".lock")
    with cc.collab_lock(lockdir, ttl=10.0, acquire_timeout=30.0) as h:
        h.assert_current()
        with open(log, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    return event
