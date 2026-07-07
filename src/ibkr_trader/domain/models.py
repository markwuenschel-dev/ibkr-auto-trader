"""domain models — frozen value objects + the type-gated constructibility seam (PT-1).

These are the objects the whole pipeline passes around, and the safety seam that makes *"a strategy can
never mint an executable order"* structural instead of a rule you have to remember:

    StrategyIntent       — a strategy proposes. Freely constructible (PT-11 emits these).
    RiskContext          — the account/market snapshot. Freely constructible (the ibkr layer builds it).
    RiskPlan             — a sized proposal with a mandatory stop. Freely constructible inside Risk & Sizing.
    ApprovedOrderIntent  — MINTED ONLY with RISK_AUTHORITY. Direct construction raises.
    ExecutableOrder      — MINTED ONLY with EXECUTION_AUTHORITY. Adapters accept nothing else.
    Fill / Ack           — adapter results. Freely constructible.

Enforcement is capability-based (the strongest practical form in Python): a guarded model blocks direct
construction and is reachable only through ``_mint(authority, ...)`` with the one unforgeable
``MintAuthority`` singleton its class requires. A strategy holds no authority, so any attempt to mint is a
loud, greppable, type-level error — not a silent slip.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class MintAuthority:
    """An unforgeable capability to mint a guarded order object. The only instances are the two
    module-level singletons below; each guarded model checks for the *specific* one it requires by
    identity, so holding the wrong authority (or none) cannot mint."""

    __slots__ = ("_label",)

    def __init__(self, label: str) -> None:
        self._label = label

    def __repr__(self) -> str:
        return f"MintAuthority({self._label!r})"


# The system's only two authorities. Risk & Sizing (PT-7) imports RISK_AUTHORITY; Execution Control
# (PT-9) imports EXECUTION_AUTHORITY. A strategy imports neither.
RISK_AUTHORITY = MintAuthority("risk-and-sizing")
EXECUTION_AUTHORITY = MintAuthority("execution-control")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# freely-constructible value objects
# --------------------------------------------------------------------------- #


class RiskContext(_Frozen):
    """The account + market snapshot a decision is made against (built by the ibkr layer, PT-3)."""

    as_of: datetime
    net_liquidation: Decimal
    buying_power: Decimal
    maintenance_margin: Decimal
    positions: dict[str, int] = Field(default_factory=dict)  # symbol -> signed shares
    prices: dict[str, Decimal] = Field(default_factory=dict)  # symbol -> last price
    data_as_of: dict[str, datetime] = Field(default_factory=dict)  # symbol -> md timestamp (causal check)


class StrategyIntent(_Frozen):
    """What a strategy proposes — never an order. The rebalancer (PT-11) emits only these."""

    symbol: str
    target_weight: Decimal  # desired portfolio weight
    rationale: str = ""


class RiskPlan(_Frozen):
    """A sized proposal carrying a mandatory stop, produced by the Risk Planner (PT-6)."""

    symbol: str
    side: Side
    quantity: int
    stop_price: Decimal
    est_risk_amount: Decimal  # $ at risk if the stop is hit


class Ack(_Frozen):
    """A broker acknowledgement (adapter result)."""

    order_id: str
    accepted_at: datetime


class Fill(_Frozen):
    """A (possibly partial) fill (adapter result)."""

    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    filled_at: datetime


# --------------------------------------------------------------------------- #
# mint-guarded objects — a strategy can never construct these
# --------------------------------------------------------------------------- #


class _MintGuarded(_Frozen):
    """A frozen model that CANNOT be constructed directly — only via ``_mint`` with the specific
    ``MintAuthority`` the subclass declares in ``_required_authority``."""

    _required_authority: ClassVar[MintAuthority]

    def __init__(self, **data: Any) -> None:
        if data.pop("_authority", None) is not type(self)._required_authority:
            raise TypeError(
                f"{type(self).__name__} cannot be constructed directly; it is minted only via "
                f"{type(self).__name__}._mint(<{type(self)._required_authority._label} authority>, ...) "
                "inside Risk & Sizing / Execution Control."
            )
        super().__init__(**data)

    @classmethod
    def _mint(cls, authority: MintAuthority, **fields: Any):
        # The identity check lives in __init__; passing the authority through proves the caller held it.
        return cls(_authority=authority, **fields)


class ApprovedOrderIntent(_MintGuarded):
    """Risk & Sizing's approval — minted ONLY with RISK_AUTHORITY. Proof an order cleared the ledger."""

    _required_authority = RISK_AUTHORITY

    symbol: str
    side: Side
    quantity: int
    stop_price: Decimal
    approved_at: datetime
    ledger_ref: str


class ExecutableOrder(_MintGuarded):
    """The only object an adapter will accept — minted ONLY with EXECUTION_AUTHORITY."""

    _required_authority = EXECUTION_AUTHORITY

    approved: ApprovedOrderIntent
    mode: str  # the config.Mode value in force at authorization time
    idempotency_key: str
