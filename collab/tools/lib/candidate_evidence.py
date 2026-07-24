"""Append-only candidate ownership, lineage, revision, and assessment evidence."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import collab_common as cc
import run_evidence

SCHEMA_VERSION = "1.0"
_FORBIDDEN = {"prompt", "output", "completion", "reasoning", "chain_of_thought"}


class CandidateEvidenceError(cc.CollabError):
    """Candidate evidence is malformed or conflicts with an immutable event."""


def _path(collab: str | Path) -> Path:
    return Path(collab) / "autopilot" / "run-events.jsonl"


def _reject_private(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN:
                raise CandidateEvidenceError(f"private model-I/O field is forbidden: {key}")
            _reject_private(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_private(child)


def _event_id(
    run_uid: str, candidate_id: str, event_type: str, timestamp: str, discriminator: str = ""
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"collab-candidate:{run_uid}:{candidate_id}:{event_type}:{timestamp}:{discriminator}",
        )
    )


def _all_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise CandidateEvidenceError("run-events.jsonl contains malformed evidence") from exc
        if not isinstance(item, dict):
            raise CandidateEvidenceError("run-events.jsonl contains a non-object record")
        records.append(item)
    return records


def append_event(collab: str | Path, event: dict[str, Any]) -> None:
    if event.get("record_type") != "candidate_evidence" or not event.get("event_id"):
        raise CandidateEvidenceError("typed candidate evidence with event_id is required")
    _reject_private(event)
    try:
        run_evidence.append(collab, event, identity_field="event_id")
    except run_evidence.RunEvidenceError as exc:
        raise CandidateEvidenceError(str(exc)) from exc


def record_created(
    collab: str | Path,
    *,
    run_uid: str,
    candidate_id: str,
    handoff_id: str,
    timestamp: str,
    producer: dict[str, Any],
    parent_candidate_id: str | None = None,
    incorporated_candidate_ids: tuple[str, ...] = (),
    task_version: str | None = None,
    base_commit: str | None = None,
    worktree_state_hash: str | None = None,
    files: tuple[str, ...] = (),
    patch_digest: str | None = None,
    tools: tuple[str, ...] = (),
    revision_evidence_refs: tuple[str, ...] = (),
    final_artifact_ref: str | None = None,
    final_commit: str | None = None,
) -> dict[str, Any]:
    event = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "candidate_evidence",
        "event_type": "candidate_created",
        "event_id": _event_id(
            run_uid,
            candidate_id,
            "candidate_created",
            timestamp,
            str(producer.get("attempt_id") or "unknown-producer-attempt"),
        ),
        "run_uid": run_uid,
        "candidate_id": candidate_id,
        "handoff_id": handoff_id,
        "timestamp": timestamp,
        "producer": dict(producer),
        "parent_candidate_id": parent_candidate_id,
        "incorporated_candidate_ids": list(incorporated_candidate_ids),
        "task_version": task_version,
        "base_commit": base_commit,
        "worktree_state_hash": worktree_state_hash,
        "files": sorted(set(files)),
        "patch_digest": patch_digest,
        "tools": list(tools),
        "revision_evidence_refs": list(revision_evidence_refs),
        "final_artifact_ref": final_artifact_ref,
        "final_commit": final_commit,
    }
    append_event(collab, event)
    return event


def record_assessed(
    collab: str | Path,
    *,
    run_uid: str,
    candidate_id: str,
    handoff_id: str,
    timestamp: str,
    outcome: str,
    evaluator: str,
    feedback_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    event = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "candidate_evidence",
        "event_type": "candidate_assessed",
        "event_id": _event_id(
            run_uid,
            candidate_id,
            "candidate_assessed",
            timestamp,
            f"{evaluator}:{outcome}",
        ),
        "run_uid": run_uid,
        "candidate_id": candidate_id,
        "handoff_id": handoff_id,
        "timestamp": timestamp,
        "outcome": outcome,
        "evaluator": evaluator,
        "feedback_refs": list(feedback_refs),
    }
    append_event(collab, event)
    return event


def read_events(collab: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return read_event_file(_path(collab), run_uid=run_uid)


def read_event_file(path: str | Path, *, run_uid: str | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in _all_records(Path(path))
        if item.get("record_type") == "candidate_evidence"
        and (run_uid is None or item.get("run_uid") == run_uid)
    ]


def project(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[(str(event.get("run_uid")), str(event.get("candidate_id")))].append(event)
    candidates = []
    for (_run_uid, _candidate_id), raw in groups.items():
        ordered = sorted(raw, key=lambda item: (str(item.get("timestamp")), str(item.get("event_id"))))
        created = next((item for item in ordered if item.get("event_type") == "candidate_created"), {})
        assessed = [item for item in ordered if item.get("event_type") == "candidate_assessed"]
        latest_assessment = assessed[-1] if assessed else {}
        candidates.append(
            {
                "run_uid": created.get("run_uid") or ordered[0].get("run_uid"),
                "candidate_id": created.get("candidate_id") or ordered[0].get("candidate_id"),
                "handoff_id": created.get("handoff_id") or ordered[0].get("handoff_id"),
                "producer": created.get("producer"),
                "parent_candidate_id": created.get("parent_candidate_id"),
                "incorporated_candidate_ids": created.get("incorporated_candidate_ids") or [],
                "task_version": created.get("task_version"),
                "base_commit": created.get("base_commit"),
                "worktree_state_hash": created.get("worktree_state_hash"),
                "files": created.get("files") or [],
                "patch_digest": created.get("patch_digest"),
                "tools": created.get("tools") or [],
                "revision_evidence_refs": created.get("revision_evidence_refs") or [],
                "final_artifact_ref": created.get("final_artifact_ref"),
                "final_commit": created.get("final_commit"),
                "evaluator_feedback": latest_assessment.get("feedback_refs") or [],
                "current_disposition": latest_assessment.get("outcome") or "produced",
                "revisions": [
                    {
                        "event_id": item.get("event_id"),
                        "event_type": item.get("event_type"),
                        "timestamp": item.get("timestamp"),
                    }
                    for item in ordered
                ],
            }
        )
    return sorted(candidates, key=lambda item: str(item.get("candidate_id")))
