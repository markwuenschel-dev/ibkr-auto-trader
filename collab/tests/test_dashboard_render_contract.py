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
        "run_archive_persistence",
        "attempt_persistence",
        "call_ledger_persistence",
        "run_evidence_persistence",
        "schema_compatibility",
        "freshness",
        "stream",
        "gateway",
        "langfuse",
    ):
        assert dimension in page
    assert "renderOperational" in page
    assert "operational_state" in page


def test_page_does_not_overload_rounds_with_attempts_or_actor_turns() -> None:
    page = dw._PAGE
    for truthful_label in (
        "Work attempt limit",
        "Max work attempts",
        "work attempt ",
        "work attempts",
        "actor turns",
        "verification calls",
        "provider attempts",
        "completed round boundaries",
        "latest_decision",
    ):
        assert truthful_label in page
    for misleading_label in (
        "round budget reached",
        '"rounds"',
        '"avg round"',
        '" rounds · "',
    ):
        assert misleading_label not in page


def test_page_has_four_keyboard_operable_coordinated_views() -> None:
    page = dw._PAGE
    assert 'role="tablist"' in page
    for view in ("operator", "models", "quality", "diagnostics"):
        assert f'id="tab-{view}"' in page
        assert f'aria-controls="view-{view}"' in page
        assert f'id="view-{view}"' in page
        assert 'role="tabpanel"' in page
    for field in (
        "operator_summary",
        "execution_roster",
        "model_activity",
        "candidates",
        "validations",
        "requirements",
        "dispositions",
        "evidence_health",
    ):
        assert field in page
    assert "activateView" in page
    assert 'e.key==="ArrowRight"' in page
    assert 'e.key==="ArrowLeft"' in page
    assert 'e.key==="Home"' in page
    assert 'e.key==="End"' in page


def test_page_declares_bounded_collection_windows_and_accessible_filters() -> None:
    page = dw._PAGE
    assert "<main" in page and "</main>" in page
    assert "collection_windows" in page
    assert 'id="modelFilter"' in page
    assert 'aria-label="Filter execution roster"' in page
    assert 'id="qualityFilter"' in page
    assert 'aria-label="Filter quality evidence window"' in page
    assert "Showing latest" in page
    assert "current retained window" in page
    assert "maxWorkAttempts" in page
    assert "provider_attempts" in page
    assert '.viewpanel[hidden]{ display:none!important; }' in page


def test_historical_view_renders_the_sealed_human_summary_and_evidence_sections() -> None:
    page = dw._PAGE
    assert '"&window=1"' in page
    for field in (
        "operator_run_summary",
        "proven_facts",
        "evaluator_judgments",
        "model_claims",
        "missing_evidence",
        "human_actions",
        "attempts",
        "candidates",
        "validations",
        "requirements",
        "dispositions",
    ):
        assert field in page
    assert "Historical human summary was not recorded" in page


def test_model_and_quality_views_expose_operator_safe_evidence_detail() -> None:
    page = dw._PAGE
    for field in (
        "selection_reason",
        "assigned_task",
        "parent_attempt_id",
        "provider_attempt_count",
        "orchestration_execution_count",
        "terminal_disposition",
        "terminal_reason",
        "telemetry_outcome",
        "started_ts",
        "completed_ts",
        "updated_ts",
        "gateway_route",
        "actual_model",
        "provider",
        "gateway_request_id",
        "provider_request_id",
        "first_token_latency_ms",
        "total_duration_ms",
        "streaming",
        "tokens",
        "cost",
        "retry_count",
        "last_activity",
        "tool_activity",
        "dimensions",
        "baseline_delta",
        "uncertainty",
        "test_quality",
        "primary_reason",
        "reason_categories",
        "failed_checks",
        "decision_maker",
        "superseded_by_candidate_id",
        "revision_evidence_refs",
        "final_artifact_ref",
        "final_commit",
    ):
        assert field in page
    assert "Private model reasoning and response bodies are not displayed" in page
    for label in (
        "Connecting to gateway",
        "Gateway accepted request",
        "Waiting for tool",
        "Waiting for evaluator",
        "Telemetry export failed",
        "Failed before invocation",
    ):
        assert label in page
    assert "tool_started" in page
    assert "tool_completed" in page
