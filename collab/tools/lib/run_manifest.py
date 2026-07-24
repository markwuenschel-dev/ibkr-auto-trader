"""Write-last cryptographic inventory for durable run archives."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collab_common as cc

SCHEMA_VERSION = "1.0"
REQUIRED = (
    "run-plan.json",
    "run.json",
    "status.json",
    "events.jsonl",
    "model-events.jsonl",
    "run-events.jsonl",
    "operator-summary.json",
)


class RunManifestError(cc.CollabError):
    """An archive cannot be sealed or its manifest is invalid."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _schema_versions(path: Path) -> list[str]:
    if path.suffix not in (".json", ".jsonl") or path.stat().st_size == 0:
        return []
    versions: set[str] = set()
    try:
        if path.suffix == ".json":
            values = [json.loads(path.read_text("utf-8"))]
        else:
            values = []
            for line in path.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    values.append(json.loads(line))
                except ValueError:
                    # The raw JSONL is still hash-bound evidence. A crash may leave one torn tail, which
                    # replay handles explicitly; sealing must retain it rather than erase or rewrite it.
                    continue
    except (OSError, ValueError) as exc:
        raise RunManifestError(f"cannot seal malformed JSON artifact: {path.name}") from exc
    for value in values:
        if isinstance(value, dict) and value.get("schema_version") is not None:
            versions.add(str(value["schema_version"]))
    return sorted(versions)


def _category(relative: str) -> str:
    if relative == "run-plan.json":
        return "plan_and_roster"
    if relative == "model-events.jsonl":
        return "model_attempts_and_telemetry"
    if relative == "run-events.jsonl":
        return "decisions_candidates_validations_dispositions_requirements"
    if relative == "events.jsonl":
        return "legacy_orchestration_events"
    if relative == "run.json":
        return "summary"
    if relative == "operator-summary.json":
        return "operator_summary"
    if relative in (
        "model-observability-health.json",
        "model-calls-health.json",
        "run-events-health.json",
    ):
        return "persistence_health"
    if relative == "status.json":
        return "terminal_status_and_health"
    if relative.startswith("verification/"):
        return "verification"
    if "narrative" in relative:
        return "narrative"
    return "supporting_evidence"


def seal(
    run_dir: str | Path,
    *,
    run_uid: str,
    partial: bool = False,
    sealed_ts: str | None = None,
) -> dict[str, Any]:
    """Inventory every retained artifact and atomically write ``manifest.json`` last."""
    root = Path(run_dir).resolve()
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing and not partial:
        raise RunManifestError("required archive artifacts are missing: " + ", ".join(missing))
    artifacts: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json" or ".tmp." in path.name or path.name.endswith(".lock"):
            continue
        artifacts.append(
            {
                "path": relative,
                "category": _category(relative),
                "size_bytes": path.stat().st_size,
                "sha256": _digest(path),
                "schema_versions": _schema_versions(path),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "run_manifest",
        "run_uid": run_uid,
        "state": "partial" if partial else "sealed",
        "sealed_ts": sealed_ts or _timestamp(),
        "artifacts": artifacts,
        "gaps": [f"missing:{name}" for name in missing],
    }
    cc.safe_write(root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify(run_dir: str | Path) -> dict[str, Any]:
    """Recompute every retained digest; any missing, changed, or escaping path is a failure."""
    root = Path(run_dir).resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {"valid": False, "failures": ["manifest_missing_or_malformed"]}
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        return {"valid": False, "failures": ["manifest_schema_incompatible"]}
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return {"valid": False, "failures": ["manifest_artifacts_invalid"]}
    failures: list[str] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            failures.append("artifact_entry_invalid")
            continue
        relative = item["path"]
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            failures.append(f"unsafe_path:{relative}")
            continue
        if not target.is_file():
            failures.append(f"missing:{relative}")
        elif _digest(target) != item.get("sha256"):
            failures.append(f"hash_mismatch:{relative}")
        elif target.stat().st_size != item.get("size_bytes"):
            failures.append(f"size_mismatch:{relative}")
    return {
        "valid": not failures,
        "state": manifest.get("state"),
        "run_uid": manifest.get("run_uid"),
        "failures": failures,
        "gaps": manifest.get("gaps") or [],
    }
