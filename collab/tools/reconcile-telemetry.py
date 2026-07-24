#!/usr/bin/env python3
"""Reconcile retained model attempts to LiteLLM-owned Langfuse generations."""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

import telemetry_reconcile as tr  # noqa: E402


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collab", type=Path, required=True)
    parser.add_argument("--run-uid")
    parser.add_argument("--from-time", required=True)
    parser.add_argument("--to-time", required=True)
    parser.add_argument("--grace-seconds", type=float, default=600.0)
    args = parser.parse_args()
    path = args.collab / "autopilot" / "model-events.jsonl"
    source = tr.LangfuseObservationSource(tr.LangfuseConfig.from_env())
    summary = tr.reconcile_source(
        path,
        source,
        started=_instant(args.from_time),
        ended=_instant(args.to_time),
        now=datetime.now(UTC),
        grace_seconds=args.grace_seconds,
        run_uid=args.run_uid,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary.get("missing") and not summary.get("verification_failed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
