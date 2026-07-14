"""handoff_events.py — self-logging handoff lifecycle helpers (ARCHITECTURE.md §7.1, §8).

Thin wrappers over :func:`trace.emit` so that *every* handoff state transition appends one
JSONL envelope line to the audit log. This is the "log first" invariant (§8: "log first;
everything depends on traces") applied to the handoff lifecycle (§7.1): a handoff moves
``create -> claim -> review -> (revise -> review)* -> done``, and each edge auto-records into
the immutable trace instead of relying on a caller to remember to emit.

Each helper picks the correct ``stage``/``role`` and the reason codes for its transition, keeps
``artifact = f"handoff:{handoff_id}"`` stable across the lifecycle so every event about a
handoff is greppable by one key, and returns the emitted event dict (the same dict
``trace.emit`` wrote), so callers can chain ``span_id``/``parent_span_id`` into the span tree.

Usage:
    from handoff_events import on_create, on_claim, on_review, on_revise, on_done
    e0 = on_create(log, "slice-02", "002-handoff-core", span_id="h1", title="handoff-core")
    e1 = on_claim(log, "slice-02", "002-handoff-core",
                  span_id="h2", parent_span_id=e0["span_id"], by="reviewer")
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_local(alias: str, filename: str):
    """Load a sibling module BY PATH — immune to stdlib name-shadowing. A plain ``import trace``
    binds whichever ``trace`` is already in ``sys.modules`` (the stdlib one, if anything imported
    it first), silently breaking ``_trace.emit`` and dropping telemetry. Loading by file path always
    binds OUR ``trace.py`` (§8 substrate)."""
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_local("collab_trace", "trace.py")


def _decision(action: str, reason_codes: list[str]) -> dict:
    """Build a ``decision`` record. ``confidence`` is null until a calibrated model exists
    (§6.1) — today authority comes from evidence, not a score (§5.6)."""
    return {"action": action, "reason_codes": list(reason_codes), "confidence": None}


def _verdict_to_action(verdict: str) -> str:
    """Map a review ``verdict`` onto a ``decision.action`` (§7.1 review edge).

    ``approved``/``authorized`` (and kin) => ``accept``; ``conditional``/``blocked`` (and
    ``revise``/``reject`` kin) => ``revise``. Raises ``ValueError`` on an unrecognized verdict
    so a mistyped gate is loud, not silently accepted.
    """
    v = verdict.strip().lower()
    # Check revise-verdicts first: "conditional_approval" contains "approv" but is NOT an accept.
    if "conditional" in v or "block" in v or v in {"revise", "reject", "rejected", "changes"}:
        return "revise"
    if "approv" in v or "authoriz" in v or v in {"accept", "accepted", "pass"}:
        return "accept"
    raise ValueError(f"unrecognized review verdict: {verdict!r}")


def on_create(log, run_id: str, handoff_id: str, *, span_id: str, title: str) -> dict:
    """Log a handoff being created/proposed (§7.1 create edge).

    stage=``handoff.create``, role=``builder``. Root of the handoff span tree, so no
    ``parent_span_id``. Returns the emitted event dict.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="handoff.create",
        role="builder",
        artifact=f"handoff:{handoff_id}",
        span_id=span_id,
        decision=_decision("handoff", [f"propose:{title}"]),
    )


def on_claim(
    log,
    run_id: str,
    handoff_id: str,
    *,
    span_id: str,
    parent_span_id: str,
    by: str,
) -> dict:
    """Log a reviewer claiming an open handoff (§7.1 claim edge).

    stage=``review``, role=``reviewer``, action=``route`` with a ``claim:<by>`` reason code.
    Returns the emitted event dict.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="review",
        role="reviewer",
        artifact=f"handoff:{handoff_id}",
        span_id=span_id,
        parent_span_id=parent_span_id,
        decision=_decision("route", [f"claim:{by}"]),
    )


def on_review(
    log,
    run_id: str,
    handoff_id: str,
    *,
    span_id: str,
    parent_span_id: str,
    verdict: str,
    reason_codes: list[str],
) -> dict:
    """Log a review verdict on a handoff (§7.1 review edge, §5 evidence oracle).

    stage=``review``, role=``reviewer``. ``decision.action`` is derived from ``verdict`` via
    :func:`_verdict_to_action` (accept|revise); the caller's ``reason_codes`` are preserved
    verbatim, and ``verdict`` is echoed under ``eval`` to keep the raw gate label. Returns the
    emitted event dict.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="review",
        role="reviewer",
        artifact=f"handoff:{handoff_id}",
        span_id=span_id,
        parent_span_id=parent_span_id,
        decision=_decision(_verdict_to_action(verdict), reason_codes),
        eval={"verdict": verdict},
    )


def on_revise(
    log,
    run_id: str,
    handoff_id: str,
    *,
    span_id: str,
    parent_span_id: str,
    revision,
    reason_codes: list[str],
) -> dict:
    """Log the builder handing back a revised handoff (§7.1 revise edge).

    stage=``handoff.revise``, role=``builder``, action=``revise``. ``revision`` is recorded as
    the ``artifact_version`` (e.g. ``"rev2"``) and the caller's ``reason_codes`` are preserved
    verbatim. Returns the emitted event dict.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="handoff.revise",
        role="builder",
        artifact=f"handoff:{handoff_id}",
        artifact_version=str(revision),
        span_id=span_id,
        parent_span_id=parent_span_id,
        decision=_decision("revise", reason_codes),
    )


def on_done(
    log,
    run_id: str,
    handoff_id: str,
    *,
    span_id: str,
    parent_span_id: str,
) -> dict:
    """Log a handoff reaching its terminal ``done`` state (§7.1 done edge).

    stage=``handoff.done``, role=``builder``, action=``accept`` with a ``claimed->done`` reason
    code and an explicit ``failure=None`` (clean close). Returns the emitted event dict.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="handoff.done",
        role="builder",
        artifact=f"handoff:{handoff_id}",
        span_id=span_id,
        parent_span_id=parent_span_id,
        decision=_decision("accept", ["claimed->done"]),
        failure=None,
    )


def on_autonomous_done(
    log,
    run_id: str,
    handoff_id: str,
    *,
    span_id: str,
    parent_span_id: str,
    reviewer: str,
    contract_hash: str,
) -> dict:
    """Log a handoff reaching ``done`` by an AUTONOMOUS reviewer sign-off whose §18.3 evidence contract
    was satisfied. A distinct stage (``handoff.autonomous_done``) from the manual/human ``handoff.done``
    edge so the audit stream separates the two; ``role`` is the approving reviewer seat (never the builder —
    separation of authority, §18); the ``contract_hash`` pins WHICH evidence verdict authorized the move.
    """
    return _trace.emit(
        log,
        run_id=run_id,
        stage="handoff.autonomous_done",
        role=reviewer,
        artifact=f"handoff:{handoff_id}",
        span_id=span_id,
        parent_span_id=parent_span_id,
        decision=_decision(
            "accept", ["claimed->done", f"reviewer:{reviewer}", f"contract:{contract_hash[:12]}"]
        ),
        failure=None,
    )
