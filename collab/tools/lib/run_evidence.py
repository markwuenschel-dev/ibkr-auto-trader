"""Health-aware append contract for shared structured run evidence."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import collab_common as cc

SCHEMA_VERSION = "1.0"


class RunEvidenceError(cc.CollabError):
    """Structured run evidence is malformed, conflicting, or could not persist."""


def path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events.jsonl"


def health_path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events-health.json"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_health(
    collab: str | Path,
    *,
    status: Literal["healthy", "unavailable"],
    run_uid: str | None,
    failed_record_id: str | None = None,
    failure_class: str | None = None,
) -> None:
    cc.safe_write(
        health_path(collab),
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "run_evidence_health",
                "run_uid": run_uid,
                "status": status,
                "updated_ts": _timestamp(),
                "reason": (
                    f"{failure_class or 'unknown_error'}: structured run-evidence append failed"
                    if status == "unavailable"
                    else None
                ),
                "failed_record_id": failed_record_id,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def read_health(collab: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(health_path(collab).read_text("utf-8"))
    except (OSError, ValueError):
        return {
            "status": "unknown",
            "updated_ts": None,
            "reason": "structured run-evidence persistence has not reported",
            "run_uid": None,
            "failed_record_id": None,
        }
    if not isinstance(value, dict):
        return {
            "status": "unavailable",
            "updated_ts": None,
            "reason": "structured run-evidence persistence health is malformed",
            "run_uid": None,
            "failed_record_id": None,
        }
    return value


def _records(target: Path) -> list[dict[str, Any]]:
    try:
        lines = target.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RunEvidenceError("run-events.jsonl is unreadable") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise RunEvidenceError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(value, dict):
            raise RunEvidenceError("run-events.jsonl contains a non-object record")
        records.append(value)
    return records


def _append_locked(
    target: Path, event: dict[str, Any], *, identity_field: str
) -> Literal["appended", "duplicate"]:
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
    identity = event[identity_field]
    record_type = event.get("record_type")
    with cc.collab_lock(target.with_name(target.name + ".lock"), ttl=10.0, acquire_timeout=30.0):
        for prior in _records(target):
            if (
                prior.get("record_type") != record_type
                or prior.get(identity_field) != identity
            ):
                continue
            if prior != event:
                raise RunEvidenceError(
                    f"{identity_field} collision for {record_type}: {identity}"
                )
            return "duplicate"
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return "appended"


def append(
    collab: str | Path,
    event: dict[str, Any],
    *,
    identity_field: str,
) -> Literal["appended", "duplicate"]:
    identity = event.get(identity_field)
    if not identity or not event.get("record_type"):
        raise RunEvidenceError("record_type and typed record identity are required")
    target = path(collab)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = _append_locked(target, event, identity_field=identity_field)
    except Exception as exc:
        with suppress(Exception):
            _write_health(
                collab,
                status="unavailable",
                run_uid=str(event.get("run_uid")) if event.get("run_uid") else None,
                failed_record_id=str(identity),
                failure_class=type(exc).__name__,
            )
        if isinstance(exc, RunEvidenceError):
            raise
        raise RunEvidenceError(
            f"run-evidence persistence failed for {identity_field} {identity}"
        ) from exc
    with suppress(Exception):
        _write_health(
            collab,
            status="healthy",
            run_uid=str(event.get("run_uid")) if event.get("run_uid") else None,
        )
    return result
