"""Sealed run-manifest integrity and recoverable partial archives."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import run_manifest as rm  # noqa: E402


def _complete_run(root: Path) -> None:
    (root / "run-plan.json").write_text(
        json.dumps({"schema_version": "1.0", "run_uid": "run-1"}) + "\n",
        encoding="utf-8",
    )
    (root / "run.json").write_text(
        json.dumps({"schema_version": "1.0", "run_uid": "run-1"}) + "\n",
        encoding="utf-8",
    )
    (root / "status.json").write_text(
        json.dumps({"schema_version": "0.1", "run_uid": "run-1"}) + "\n",
        encoding="utf-8",
    )
    (root / "events.jsonl").write_text(
        json.dumps({"schema_version": "1.0", "run_id": "run-1"}) + "\n",
        encoding="utf-8",
    )
    (root / "model-events.jsonl").write_text("", encoding="utf-8")
    (root / "run-events.jsonl").write_text("", encoding="utf-8")
    (root / "operator-summary.json").write_text(
        json.dumps({"schema_version": "1.0", "run_uid": "run-1"}) + "\n",
        encoding="utf-8",
    )


def test_seal_inventory_is_hash_bound_and_manifest_is_not_self_included(tmp_path: Path) -> None:
    _complete_run(tmp_path)
    manifest = rm.seal(tmp_path, run_uid="run-1", sealed_ts="2026-07-22T12:00:00Z")

    assert manifest["state"] == "sealed"
    assert manifest["gaps"] == []
    paths = [item["path"] for item in manifest["artifacts"]]
    assert paths == sorted(paths)
    assert "manifest.json" not in paths
    assert all(len(item["sha256"]) == 64 and item["size_bytes"] >= 0 for item in manifest["artifacts"])
    assert rm.verify(tmp_path)["valid"] is True


def test_verify_fails_closed_after_artifact_mutation(tmp_path: Path) -> None:
    _complete_run(tmp_path)
    rm.seal(tmp_path, run_uid="run-1")
    (tmp_path / "status.json").write_text('{"run_uid":"tampered"}\n', encoding="utf-8")

    result = rm.verify(tmp_path)
    assert result["valid"] is False
    assert result["failures"] == ["hash_mismatch:status.json"]


def test_missing_required_artifact_refuses_full_seal_but_partial_names_gap(tmp_path: Path) -> None:
    (tmp_path / "status.json").write_text('{"run_uid":"run-1"}\n', encoding="utf-8")
    with pytest.raises(rm.RunManifestError, match="required archive artifacts"):
        rm.seal(tmp_path, run_uid="run-1")

    manifest = rm.seal(tmp_path, run_uid="run-1", partial=True)
    assert manifest["state"] == "partial"
    assert "missing:run-plan.json" in manifest["gaps"]
    assert "missing:run.json" in manifest["gaps"]
    assert rm.verify(tmp_path)["valid"] is True


def test_verify_rejects_manifest_path_escape(tmp_path: Path) -> None:
    _complete_run(tmp_path)
    manifest = rm.seal(tmp_path, run_uid="run-1")
    manifest["artifacts"][0]["path"] = "../secret"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert rm.verify(tmp_path)["failures"] == ["unsafe_path:../secret"]
