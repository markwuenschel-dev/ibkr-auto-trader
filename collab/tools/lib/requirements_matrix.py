"""Immutable requirement coverage with source authority and named acceptance oracle."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import collab_common as cc
import run_evidence

SCHEMA_VERSION = "1.0"
STATUSES = {"met", "partial", "missing", "unknown", "not_applicable"}


class RequirementsMatrixError(cc.CollabError):
    """Requirement evidence is malformed or conflicts with immutable evidence."""


def _path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events.jsonl"


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise RequirementsMatrixError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(item, dict):
            raise RequirementsMatrixError("run-events.jsonl contains a non-object record")
        out.append(item)
    return out


def _append(collab: str | Path, event: dict[str, Any]) -> None:
    try:
        run_evidence.append(collab, event, identity_field="event_id")
    except run_evidence.RunEvidenceError as exc:
        raise RequirementsMatrixError(str(exc)) from exc


def record_requirement(
    collab: str | Path,
    *,
    run_uid: str,
    candidate_id: str,
    requirement_id: str,
    description: str,
    critical: bool,
    status: str,
    source_kind: str,
    evidence_refs: tuple[str, ...],
    producer: str,
    producer_version: str,
    timestamp: str,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise RequirementsMatrixError(f"unknown requirement status: {status}")
    if status == "met" and not evidence_refs:
        raise RequirementsMatrixError("a met requirement requires evidence")
    identity = f"{run_uid}:{candidate_id}:{requirement_id}:{timestamp}:{source_kind}"
    event = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "requirement_evidence",
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "collab-requirement:" + identity)),
        "run_uid": run_uid,
        "candidate_id": candidate_id,
        "requirement_id": requirement_id,
        "description": description,
        "critical": bool(critical),
        "status": status,
        "source_kind": source_kind,
        "evidence_refs": list(evidence_refs),
        "producer": producer,
        "producer_version": producer_version,
        "timestamp": timestamp,
    }
    _append(collab, event)
    return event


def read_requirements(collab: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return read_requirement_file(_path(collab), run_uid=run_uid)


def read_requirement_file(path: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in _records(Path(path))
        if item.get("record_type") == "requirement_evidence"
        and (run_uid is None or item.get("run_uid") == run_uid)
    ]


def project(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: str(item.get("timestamp"))):
        latest[(str(record.get("candidate_id")), str(record.get("requirement_id")))] = record
    out = []
    for record in latest.values():
        effective = record.get("status")
        if record.get("critical") and record.get("source_kind") == "model_self_report":
            effective = "unverified"
        out.append({**record, "effective_status": effective})
    return sorted(out, key=lambda item: (str(item.get("candidate_id")), str(item.get("requirement_id"))))


def evaluate_acceptance(
    records: list[dict[str, Any]],
    *,
    validations: list[dict[str, Any]],
    candidate_id: str,
    oracle_validation_id: str,
) -> dict[str, Any]:
    matrix = [item for item in project(records) if item.get("candidate_id") == candidate_id]
    critical = [item for item in matrix if item.get("critical")]
    unmet = [
        str(item.get("requirement_id"))
        for item in critical
        if item.get("effective_status") != "met"
    ]
    oracle = next(
        (
            item
            for item in validations
            if item.get("candidate_id") == candidate_id
            and item.get("validation_id") == oracle_validation_id
            and item.get("is_acceptance_oracle")
            and item.get("source_kind") in ("automated_check", "human_decision")
        ),
        None,
    )
    oracle_status = str(oracle.get("status")) if oracle else "missing"
    critical_met = len(critical) - len(unmet)
    return {
        "eligible": not unmet and bool(critical) and oracle_status == "passed",
        "critical_total": len(critical),
        "critical_met": critical_met,
        "unmet": unmet,
        "oracle_validation_id": oracle_validation_id,
        "oracle_status": oracle_status,
    }
