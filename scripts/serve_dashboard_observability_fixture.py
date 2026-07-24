"""Serve a deterministic four-model observability fixture for browser/operator proof.

This is deliberately test-only: it never invokes a model or provider and writes only below
the explicit ``--fixture`` and ``--home`` directories supplied by the caller.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "collab" / "tools" / "lib"
sys.path.insert(0, str(LIB))

import autopilot as ap  # noqa: E402
import candidate_disposition as cd  # noqa: E402
import candidate_evidence as ce  # noqa: E402
import collab_common as cc  # noqa: E402
import dashboard_web as dw  # noqa: E402
import handoff_core as hc  # noqa: E402
import model_observability as mo  # noqa: E402
import quality_evidence as qe  # noqa: E402
import requirements_matrix as req  # noqa: E402
import round_decision as rd  # noqa: E402
import run_budget as rb  # noqa: E402
import run_history as rh  # noqa: E402
import run_plan as rp  # noqa: E402

RUN_UID = "browser-four-model-20260722"
HANDOFF_ID = "001"
TIMESTAMPS = [f"2026-07-22T14:00:{second:02d}Z" for second in range(40)]


def _seat(model: str, role: str) -> dict:
    return {
        "backend": "cli",
        "role": role,
        "model": model,
        "cmd": ["fixture-model", model],
    }


def _model_event(
    *,
    index: int,
    role: str,
    model: str,
    state: mo.LifecycleState,
    offset: int,
    candidate_id: str,
    source: str = "gateway_client",
    telemetry_result: str | None = None,
) -> mo.ModelAttemptEvent:
    attempt_id = f"fixture-attempt-{index}"
    request_id = f"fixture-request-{index}"
    provider = {
        "gpt-5.6-luna": "openai",
        "grok-4.5": "xai",
        "gemini-3.5-flash": "google",
        "haiku-4.5": "anthropic",
    }[model]
    endpoint = "chat/completions" if model == "haiku-4.5" else "responses"
    return mo.ModelAttemptEvent(
        event_id=f"fixture-event-{index}-{offset}",
        attempt_id=attempt_id,
        request_id=request_id,
        run_uid=RUN_UID,
        seat=role,
        requested_model=model,
        state=state,
        event_ts=TIMESTAMPS[index * 5 + offset],
        attempt_number=1,
        source=source,
        handoff_id=HANDOFF_ID,
        candidate_id=candidate_id,
        gateway_route=endpoint,
        gateway_request_id=f"gateway-{index}" if offset >= 1 else None,
        provider_request_id=f"provider-{index}" if state == "completed" else None,
        actual_model=model if state == "completed" else None,
        provider=provider if state == "completed" else None,
        completion_status="completed" if state == "completed" else None,
        first_token_latency_ms=(
            80.0 + index * 10 if state in ("streaming", "completed") else None
        ),
        total_duration_ms=420.0 + index * 25 if state == "completed" else None,
        streaming=True,
        tokens={"input": 20 + index, "output": 8 + index, "cached": index, "total": 28 + index * 2}
        if state == "completed"
        else None,
        cost=0.001 + index * 0.0002 if state == "completed" else None,
        telemetry_result=telemetry_result,
        observation_id=f"observation-{index}" if state == "telemetry_verified" else None,
        trace_id=f"trace-{index}" if state == "telemetry_verified" else None,
        detail=(
            {
                "phase": "response_in_progress",
                "chunk_count": 1,
                "last_chunk_ts": TIMESTAMPS[index * 5 + offset],
            }
            if state == "streaming"
            else {
                "phase": "response_complete",
                "chunk_count": 2 + index,
                "last_chunk_ts": TIMESTAMPS[index * 5 + offset - 1],
            }
            if state == "completed"
            else {}
        ),
    )


def create_fixture(fixture: Path, home: Path) -> hc.ActiveHandoffLease:
    fixture.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    seats = {
        "builder": _seat("gpt-5.6-luna", "builder"),
        "reviewer": _seat("grok-4.5", "reviewer"),
        "breaker": _seat("gemini-3.5-flash", "breaker"),
        "verifier": _seat("haiku-4.5", "verifier"),
    }
    home_config = {
        "version": 2,
        "models": {
            config["model"]: {"cmd": config["cmd"]} for config in seats.values()
        },
        "seats": {role: {k: v for k, v in config.items() if k != "cmd"} for role, config in seats.items()},
    }
    cc.safe_write(home / "seats.json", json.dumps(home_config, indent=2) + "\n")

    if hc.state_of(fixture, HANDOFF_ID) is None:
        hid = hc.create(
            fixture,
            to="reviewer",
            from_="builder",
            title="Four-model observability proof",
            body="Evaluate two candidates and retain the rejection, acceptance, and telemetry evidence.",
            constraints=[("OBS-1", "all four planned models remain visible")],
        )["id"]
        if hid != HANDOFF_ID:
            raise RuntimeError(f"fixture expected handoff {HANDOFF_ID}, got {hid}")
        hc.claim(fixture, HANDOFF_ID)

    limits = rb.Limits(
        max_work_attempts=3,
        max_verification_passes=8,
        max_total_model_calls=16,
        max_wall_clock_seconds=900,
        max_findings_per_lane=3,
    )
    rp.declare(
        fixture,
        run_uid=RUN_UID,
        seats=seats,
        limits=limits,
        objective="Prove human-readable four-model run observability",
        created_ts=TIMESTAMPS[0],
    )

    model_log = fixture / "autopilot" / "model-events.jsonl"
    model_specs = [
        ("builder", "gpt-5.6-luna", "candidate-rejected"),
        ("reviewer", "grok-4.5", "candidate-rejected"),
        ("breaker", "gemini-3.5-flash", "candidate-accepted"),
        ("verifier", "haiku-4.5", "candidate-accepted"),
    ]
    for index, (role, model, candidate) in enumerate(model_specs):
        for offset, state in enumerate(
            ("connecting", "gateway_accepted", "streaming", "completed", "telemetry_verified")
        ):
            mo.append_event(
                model_log,
                _model_event(
                    index=index,
                    role=role,
                    model=model,
                    state=state,
                    offset=offset,
                    candidate_id=candidate,
                    source="langfuse_reconciler" if state == "telemetry_verified" else "gateway_client",
                    telemetry_result="verified" if state == "telemetry_verified" else None,
                ),
            )
    mo._write_persistence_health(model_log, "healthy", run_uid=RUN_UID)

    ce.record_created(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-rejected",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[20],
        producer={"role": "builder", "model": "gpt-5.6-luna", "attempt_id": "fixture-attempt-0"},
        task_version="dashboard-observability-v1",
        base_commit="fixture-base",
        files=("collab/tools/lib/dashboard_web.py",),
        tools=("apply_patch", "pytest"),
        final_artifact_ref="fixture-patch:candidate-rejected",
    )
    ce.record_assessed(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-rejected",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[21],
        outcome="rejected",
        evaluator="grok-4.5",
        feedback_refs=("validation:rejected-browser-check",),
    )
    ce.record_created(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-accepted",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[22],
        producer={"role": "builder", "model": "gpt-5.6-luna", "attempt_id": "fixture-attempt-2"},
        parent_candidate_id="candidate-rejected",
        incorporated_candidate_ids=("candidate-rejected",),
        task_version="dashboard-observability-v2",
        base_commit="fixture-base",
        files=("collab/tools/lib/dashboard_web.py", "collab/tools/lib/dashboard_core.py"),
        tools=("apply_patch", "pytest", "playwright"),
        revision_evidence_refs=("validation:rejected-browser-check",),
        final_artifact_ref="fixture-patch:candidate-accepted",
    )
    ce.record_assessed(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-accepted",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[23],
        outcome="accepted",
        evaluator="grok-4.5",
        feedback_refs=("validation:accepted-browser-check",),
    )

    rejected_validation = qe.record_validation(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-rejected",
        validation_id="rejected-browser-check",
        timestamp=TIMESTAMPS[24],
        source_kind="automated_check",
        status="failed",
        producer="pytest",
        producer_version="fixture-v1",
        artifact_ref="test_dashboard_render_contract.py",
        dimensions={"correctness": "failed", "accessibility": "warning"},
        uncertainty="none",
        gaps=("keyboard tab persistence failed",),
        proves_changed_behavior=True,
        fails_before_fix=True,
        exercises_degraded_modes=True,
        avoids_over_mocking=True,
        detects_negative_variants=True,
    )
    accepted_validation = qe.record_validation(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-accepted",
        validation_id="accepted-browser-check",
        timestamp=TIMESTAMPS[25],
        source_kind="automated_check",
        status="passed",
        producer="playwright",
        producer_version="fixture-v1",
        artifact_ref="dashboard/e2e/collab-observability.spec.ts",
        dimensions={"correctness": "passed", "accessibility": "passed"},
        uncertainty="browser fixture; provider calls are separately evidenced",
        proves_changed_behavior=True,
        fails_before_fix=True,
        exercises_degraded_modes=True,
        avoids_over_mocking=True,
        detects_negative_variants=True,
        is_acceptance_oracle=True,
    )
    rejected_req = req.record_requirement(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-rejected",
        requirement_id="keyboard-navigation",
        description="All dashboard views are keyboard operable",
        critical=True,
        status="missing",
        source_kind="automated_check",
        evidence_refs=(f"validation:{rejected_validation['validation_id']}",),
        producer="pytest",
        producer_version="fixture-v1",
        timestamp=TIMESTAMPS[26],
    )
    accepted_req = req.record_requirement(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-accepted",
        requirement_id="keyboard-navigation",
        description="All dashboard views are keyboard operable",
        critical=True,
        status="met",
        source_kind="automated_check",
        evidence_refs=(f"validation:{accepted_validation['validation_id']}",),
        producer="playwright",
        producer_version="fixture-v1",
        timestamp=TIMESTAMPS[27],
    )
    cd.record_disposition(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-rejected",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[28],
        disposition="rejected",
        executive_explanation=(
            "Keyboard persistence failed, so the candidate could not satisfy the operator contract."
        ),
        evidence_refs=(f"requirement:{rejected_req['requirement_id']}",),
        impact="Keyboard-only operators would lose context after reload.",
        remediation="Persist the selected view and restore focus semantics.",
        retryable=True,
        final=True,
        retained_work=("four-view layout",),
        alternatives=("single long page",),
        weaknesses=("tab selection was not durable",),
        unavailable_evidence=(),
        confidence="high",
        disagreements=({"evaluator": "builder", "position": "layout was otherwise usable"},),
        resolution="Reviewer rejection retained because the critical automated check failed.",
        human_review_triggers=("override critical accessibility failure",),
        decision_maker="grok-4.5 reviewer plus automated browser oracle",
        primary_reason="tests_failed",
        failed_checks=("rejected-browser-check", "keyboard-navigation"),
        round_number=1,
    )
    evaluation = req.evaluate_acceptance(
        [accepted_req],
        validations=[accepted_validation],
        candidate_id="candidate-accepted",
        oracle_validation_id="accepted-browser-check",
    )
    cd.record_disposition(
        fixture,
        run_uid=RUN_UID,
        candidate_id="candidate-accepted",
        handoff_id=HANDOFF_ID,
        timestamp=TIMESTAMPS[29],
        disposition="accepted",
        executive_explanation=(
            "The corrected candidate passed the named browser oracle and all critical requirements."
        ),
        evidence_refs=("validation:accepted-browser-check", "requirement:keyboard-navigation"),
        impact="Operators can move between all views and retain context after reload.",
        remediation="None required; retain the fixture limitation as visible uncertainty.",
        retryable=False,
        final=True,
        retained_work=("four-view layout", "bounded evidence windows"),
        alternatives=("single long page",),
        weaknesses=("fixture does not make provider calls",),
        unavailable_evidence=("fresh xAI provider success",),
        confidence="high",
        disagreements=(),
        resolution="Named oracle and critical requirement evidence agree.",
        human_review_triggers=("provider evidence changes",),
        requirements_evaluation=evaluation,
    )

    first_decision = rd.evaluate(
        rd.DecisionContext(
            run_uid=RUN_UID,
            completed_round=1,
            maximum_rounds=3,
            candidate_id="candidate-rejected",
            unresolved_requirements=("keyboard-navigation",),
            viable_models=("gpt-5.6-luna", "grok-4.5", "gemini-3.5-flash", "haiku-4.5"),
            remaining_budget={"work_attempts": 2, "model_calls": 12},
            timestamp=TIMESTAMPS[30],
            supporting_evidence=("disposition:candidate-rejected",),
        )
    )
    rd.persist(fixture, first_decision)
    terminal_decision = rd.evaluate(
        rd.DecisionContext(
            run_uid=RUN_UID,
            completed_round=2,
            maximum_rounds=3,
            candidate_id="candidate-accepted",
            accepted=True,
            completion_criteria_met=True,
            viable_models=("gpt-5.6-luna", "grok-4.5", "gemini-3.5-flash", "haiku-4.5"),
            remaining_budget={"work_attempts": 1, "model_calls": 8},
            timestamp=TIMESTAMPS[31],
            supporting_evidence=("disposition:candidate-accepted", "validation:accepted-browser-check"),
        )
    )
    rd.persist(fixture, terminal_decision)

    trace_path = fixture / "logs" / "events.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_rows = [
        {
            "ts": TIMESTAMPS[32 + index],
            "stage": "autopilot.round",
            "role": role,
            "artifact": f"handoff:{HANDOFF_ID}",
            "decision": {"action": "turn", "reason_codes": [f"seat:{role}"]},
            "metrics": {"latency_ms": 120 + index * 20, "resp_bytes": 100},
        }
        for index, role in enumerate(("builder", "reviewer", "breaker", "verifier"))
    ]
    cc.safe_write(trace_path, "".join(json.dumps(row) + "\n" for row in trace_rows))

    status_fields = {
        "run_uid": RUN_UID,
        "current_hid": HANDOFF_ID,
        "stage": "verify",
        "round": 2,
        "started_ts": TIMESTAMPS[0],
        "active_since": TIMESTAMPS[31],
        "timeout": 900,
        "max_rounds": 3,
        "budget": {
            "budgets": {
                "work_attempts": {"consumed": 2, "limit": 3},
                "actor_turns": {"consumed": 4},
                "verification_calls": {"consumed": 2},
            }
        },
        "run_seats": {role: config["model"] for role, config in seats.items()},
    }
    ap._write_status(
        fixture,
        **status_fields,
        phase="done",
        active_seat=None,
        ended_ts=TIMESTAMPS[36],
    )
    archive = rh.archive_run(fixture, RUN_UID)
    if archive is None:
        raise RuntimeError("fixture archive could not be created")
    ap._write_status(
        fixture,
        **status_fields,
        phase="done",
        active_seat=None,
        ended_ts=TIMESTAMPS[36],
    )
    lease = hc.ActiveHandoffLease(fixture, RUN_UID, pid=os.getpid())
    lease.acquire(HANDOFF_ID)
    return lease


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    lease = create_fixture(args.fixture.resolve(), args.home.resolve())
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(5):
            if not lease.renew():
                return
            ap._write_status(args.fixture.resolve())

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        return dw.serve(args.fixture.resolve(), home=args.home.resolve(), port=args.port)
    finally:
        stop.set()
        lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
