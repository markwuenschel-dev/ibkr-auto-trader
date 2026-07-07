"""pack — the §12 domain-pack declaration for trading (the seam to the reusable core).

A domain pack is *thin*: it inherits all control flow, risk math, telemetry, and promotion machinery
from the core and supplies only domain gates + a checklet catalog. This file makes the §12 interface
real from PT-0 — most lists are empty stubs that the pipeline slices fill in (deterministic_checks and
executables grow with PT-1…PT-15). What matters now is the two load-bearing declarations:

  * ``oracle = "execution"`` — trading has a sound oracle (tests + deterministic risk math), so per §5.7
    the verifier authority ceiling is **auto-block** and R_fw is a true-correctness bound, not merely an
    agreement bound. This is why the trader is a *safe* first pack on a fail-closed substrate.
  * the account-level ``acceptance_thresholds`` — the PROTOCOL hard limits the Rules Ledger (PT-5) enforces.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DomainPack:
    name: str
    oracle: str  # "execution" | "structural" | "oracle_poor" (§5.7 ceiling)
    artifact_types: tuple[str, ...] = ()
    deterministic_checks: tuple[str, ...] = ()  # L1 (total functions -> pass/fail); grows per slice
    static_analyzers: tuple[str, ...] = ()  # L2
    executables: tuple[str, ...] = ()  # L3 (tests/sims); the oracle
    source_rules: tuple[str, ...] = ()  # L4 (empty — trading has no external-claim layer)
    rubric_dimensions: tuple[str, ...] = ()  # L5 residual subjective (minimal for a coding pack)
    human_review_triggers: tuple[str, ...] = ()  # L6
    checklet_catalog: tuple[dict, ...] = ()  # each: criterion/input/output/authority/calibration_target
    acceptance_thresholds: dict = field(default_factory=dict)
    known_failure_modes: tuple[str, ...] = ()
    default_loop: tuple[str, ...] = ()  # recommended role set + ordering


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
        # PROTOCOL.md hard limits (mirrored in config.RiskLimits; the Rules Ledger enforces them).
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
