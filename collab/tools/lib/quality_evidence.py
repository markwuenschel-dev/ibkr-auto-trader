"""Typed validation evidence that preserves source authority and test quality."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import collab_common as cc
import run_evidence

SCHEMA_VERSION = "1.0"
SOURCE_KINDS = {
    "automated_check",
    "evaluator_judgment",
    "model_self_report",
    "human_decision",
}
STATUSES = {"passed", "failed", "warning", "unknown", "not_run"}


class QualityEvidenceError(cc.CollabError):
    """Validation evidence is invalid or conflicts with immutable evidence."""


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
            raise QualityEvidenceError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(item, dict):
            raise QualityEvidenceError("run-events.jsonl contains a non-object record")
        out.append(item)
    return out


def _append(collab: str | Path, event: dict[str, Any]) -> None:
    try:
        run_evidence.append(collab, event, identity_field="event_id")
    except run_evidence.RunEvidenceError as exc:
        raise QualityEvidenceError(str(exc)) from exc


def record_validation(
    collab: str | Path,
    *,
    run_uid: str,
    candidate_id: str,
    validation_id: str,
    timestamp: str,
    source_kind: str,
    status: str,
    producer: str,
    producer_version: str,
    artifact_ref: str,
    dimensions: dict[str, str],
    baseline_delta: dict[str, Any] | None = None,
    uncertainty: str,
    gaps: tuple[str, ...] = (),
    proves_changed_behavior: bool | None = None,
    fails_before_fix: bool | None = None,
    exercises_degraded_modes: bool | None = None,
    avoids_over_mocking: bool | None = None,
    detects_negative_variants: bool | None = None,
    is_acceptance_oracle: bool = False,
) -> dict[str, Any]:
    if source_kind not in SOURCE_KINDS or status not in STATUSES:
        raise QualityEvidenceError("source_kind or status is not recognized")
    if source_kind == "model_self_report" and is_acceptance_oracle:
        raise QualityEvidenceError("model self-report cannot be an acceptance oracle")
    if not all((run_uid, candidate_id, validation_id, timestamp, producer, producer_version, artifact_ref)):
        raise QualityEvidenceError(
            "validation identity, producer, version, artifact, and timestamp are required"
        )
    test_quality = None
    if source_kind == "automated_check":
        test_quality = {
            "proves_changed_behavior": proves_changed_behavior,
            "fails_before_fix": fails_before_fix,
            "exercises_degraded_modes": exercises_degraded_modes,
            "avoids_over_mocking": avoids_over_mocking,
            "detects_negative_variants": detects_negative_variants,
        }
    identity = f"{run_uid}:{candidate_id}:{validation_id}:{timestamp}"
    event = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "validation_evidence",
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "collab-validation:" + identity)),
        "run_uid": run_uid,
        "candidate_id": candidate_id,
        "validation_id": validation_id,
        "timestamp": timestamp,
        "source_kind": source_kind,
        "status": status,
        "producer": producer,
        "producer_version": producer_version,
        "artifact_ref": artifact_ref,
        "dimensions": dict(dimensions),
        "baseline_delta": baseline_delta,
        "uncertainty": uncertainty,
        "gaps": list(gaps),
        "test_quality": test_quality,
        "is_acceptance_oracle": bool(is_acceptance_oracle),
    }
    _append(collab, event)
    return event


def read_validations(collab: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return read_validation_file(_path(collab), run_uid=run_uid)


def read_validation_file(path: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in _records(Path(path))
        if item.get("record_type") == "validation_evidence"
        and (run_uid is None or item.get("run_uid") == run_uid)
    ]
