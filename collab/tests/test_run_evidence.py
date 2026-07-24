"""Shared durable run-evidence append and persistence-health contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import run_evidence as revidence  # noqa: E402


def _event() -> dict:
    return {
        "schema_version": "1.0",
        "record_type": "candidate_evidence",
        "event_id": "event-1",
        "run_uid": "run-1",
        "candidate_id": "candidate-1",
    }


def test_shared_append_is_idempotent_and_reports_healthy(tmp_path: Path) -> None:
    assert revidence.append(tmp_path, _event(), identity_field="event_id") == "appended"
    assert revidence.append(tmp_path, _event(), identity_field="event_id") == "duplicate"
    health = revidence.read_health(tmp_path)
    assert health["status"] == "healthy"
    assert health["run_uid"] == "run-1"


def test_shared_append_failure_is_durable_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise OSError("private storage path")

    monkeypatch.setattr(revidence, "_append_locked", fail)
    with pytest.raises(revidence.RunEvidenceError, match="run-evidence persistence failed"):
        revidence.append(tmp_path, _event(), identity_field="event_id")

    health = revidence.read_health(tmp_path)
    assert health["status"] == "unavailable"
    assert health["failed_record_id"] == "event-1"
    assert health["reason"] == "OSError: structured run-evidence append failed"
    assert str(tmp_path) not in health["reason"]
