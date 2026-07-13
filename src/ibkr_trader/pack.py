"""The declared collab-kit domain pack for the trading system.

A pack is deliberately declarative: it names authority, artifacts, executable checks, and known failure
modes before pipeline code exists. The orchestrator reads this rather than allowing an agent to invent its
own acceptance bar mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DomainPack:
    """Minimal executable-pack declaration consumed by the orchestrator."""

    name: str
    oracle: str
    artifact_types: tuple[str, ...]
    executables: tuple[str, ...]
    human_review_triggers: tuple[str, ...]
    acceptance_thresholds: dict[str, Any]
    known_failure_modes: tuple[str, ...]
    default_loop: tuple[str, ...]


TRADING_PACK = DomainPack(
    name="ibkr-trading",
    oracle="execution",
    artifact_types=(
        "domain_model",
        "risk_context",
        "strategy_intent",
        "risk_plan",
        "approved_order_intent",
        "executable_order",
        "fill_or_ack",
        "audit_record",
    ),
    executables=("pytest",),  # the risk-math + invariant suite; property-based tests land per slice
    human_review_triggers=(
        "mode_transition_to_live",  # any step toward live is human-gated
        "kill_switch_or_pause_change",
        "risk_limit_config_change",
    ),
    acceptance_thresholds={
        # PROTOCOL.md hard limits (mirrored in config.RiskPolicy; the Rules Ledger enforces them).
        "max_risk_per_trade": 0.01,
        "daily_loss_lockout": 0.03,
        "leverage_cap": 1.5,
        "stop_loss_required": True,
        "paper_default": True,
        "live_rejected_by_default": True,
    },
    known_failure_modes=(
        "order_without_stop",
        "live_leak_from_paper_mode",
        "strategy_mints_executable_order",
        "causal_data_violation_lookahead",
        "duplicate_order_no_idempotency",
        "daily_loss_lockout_not_persisted_across_restart",
        "decision_not_audited",
    ),
    default_loop=("orchestrator", "builder", "grounded_verifier"),
)
