"""Read-only reproducer for dashboard run-observability contract gaps.

This script deliberately reads only retained orchestration metadata. It never
loads model prompts or responses and never mutates the collab board.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLAB_LIB = REPO_ROOT / "collab" / "tools" / "lib"
sys.path.insert(0, str(COLLAB_LIB))

import dashboard_core  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _within(row: dict[str, Any], started: datetime | None, ended: datetime | None) -> bool:
    instant = _instant(row.get("started_ts"))
    if instant is None:
        return False
    return (started is None or instant >= started) and (ended is None or instant <= ended)


def inspect(collab_root: Path, run_uid: str, model_log: Path) -> dict[str, Any]:
    run_dir = collab_root / "history" / run_uid
    run = _json(run_dir / "run.json")
    status = _json(run_dir / "status.json")
    detail = dashboard_core.run_detail(collab_root, run_uid)
    events = _jsonl(run_dir / "events.jsonl")
    attempts = [
        row
        for row in _jsonl(model_log)
        if row.get("record_type") == "attempt"
        and _within(row, _instant(run.get("started_ts")), _instant(run.get("ended_ts")))
    ]

    raw_seats = run.get("seats")
    seats: dict[str, Any] = raw_seats if isinstance(raw_seats, dict) else {}
    planned_models = {str(model) for model in seats.values() if model}
    observed_models = {
        str(row.get("requested_model")) for row in attempts if row.get("requested_model")
    }
    event_kinds = Counter(
        f"{row.get('stage') or '?'}:{(row.get('decision') or {}).get('action') or '?'}"
        for row in events
        if isinstance(row.get("decision"), dict)
    )
    attempt_groups = Counter(
        (
            str(row.get("requested_model") or "unknown"),
            str(row.get("seat") or "unknown"),
            str(row.get("outcome") or "unknown"),
            str(row.get("run_uid") or "missing"),
        )
        for row in attempts
    )

    contracts = {
        "archive_preserves_budget": "budget" in run,
        "dashboard_detail_preserves_attempts": "attempts" in detail,
        "attempts_correlated_to_archived_run": bool(attempts)
        and all(row.get("run_uid") == run_uid for row in attempts),
        "every_planned_model_has_an_attempt": planned_models <= observed_models,
        "events_include_model_lifecycle": any(
            str(row.get("stage") or "").startswith("model.") for row in events
        ),
    }
    return {
        "run_uid": run_uid,
        "terminal": {
            "phase_final": run.get("phase_final"),
            "last_error": status.get("last_error"),
            "rounds_total": run.get("rounds_total"),
            "max_rounds": run.get("max_rounds"),
        },
        "archive_files": sorted(path.name for path in run_dir.iterdir() if path.is_file()),
        "archive_summary_keys": sorted(run),
        "status_only_keys": sorted(set(status) - set(run)),
        "dashboard_detail_keys": sorted(detail),
        "events": {"count": len(events), "kinds": dict(sorted(event_kinds.items()))},
        "attempts": {
            "count": len(attempts),
            "groups": [
                {
                    "model": model,
                    "seat": seat,
                    "outcome": outcome,
                    "run_uid": attempt_run_uid,
                    "count": count,
                }
                for (model, seat, outcome, attempt_run_uid), count in sorted(attempt_groups.items())
            ],
        },
        "roster": {
            "planned": seats,
            "observed_models": sorted(observed_models),
            "unobserved_models": sorted(planned_models - observed_models),
        },
        "contracts": contracts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collab-root", type=Path, default=REPO_ROOT / "autopilot")
    parser.add_argument("--run-uid", required=True)
    parser.add_argument("--model-log", type=Path)
    args = parser.parse_args()
    model_log = args.model_log or args.collab_root / "model-calls.jsonl"
    result = inspect(args.collab_root.resolve(), args.run_uid, model_log.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if all(result["contracts"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
