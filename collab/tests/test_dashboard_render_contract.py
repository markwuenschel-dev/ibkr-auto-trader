"""Static render contract for canonical operational fields and live recovery."""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import dashboard_web as dw  # noqa: E402


def test_page_has_a_rendering_path_for_every_operational_field_and_state() -> None:
    page = dw._PAGE
    for state in (
        "queued",
        "claimed",
        "running",
        "awaiting",
        "paused",
        "capped",
        "blocked",
        "parked",
        "escalated",
        "retrying",
        "failed",
        "cancelled",
        "superseded",
        "completed",
    ):
        assert state in page
    for field in (
        "operational_state",
        "state_reason",
        "state_source",
        "state_ts",
        "owner",
        "actor",
        "required_action",
        "severity",
        "run_id",
        "correlation_id",
        "trace_id",
        "conflicts",
        "freshness",
    ):
        assert field in page


def test_page_uses_stream_primary_with_bounded_reconnect_and_snapshot_safety_net() -> None:
    page = dw._PAGE
    assert 'new EventSource("/api/stream' in page
    assert "[1000,2000,4000,8000,16000,30000]" in page
    assert "event.lastEventId" in page
    assert "setInterval(reconcileState,30000)" in page
    assert "setInterval(refresh,2000)" not in page
    assert all(label in page for label in ("CONNECTED", "RECONNECTING", "STALE", "DISCONNECTED"))


def test_page_renders_separate_health_dimensions_and_non_color_state_text() -> None:
    page = dw._PAGE
    for dimension in (
        "source_reads",
        "reconciliation",
        "history_persistence",
        "schema_compatibility",
        "freshness",
        "stream",
        "gateway",
        "langfuse",
    ):
        assert dimension in page
    assert "renderOperational" in page
    assert "operational_state" in page
