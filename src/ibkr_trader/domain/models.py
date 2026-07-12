"""Frozen domain facts and their designated-issuer mint seams.

``_MintGuarded`` provides provenance: only a designated issuer may construct a
 guarded fact.  It is deliberately not an in-process Python security boundary:
``model_construct()`` and imports of a public authority can bypass it.  Those
holes are release-gate concerns, not claims this seam makes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# IBKR's stable contract identifier is the decision-universe set key.  This is
# an alias rather than a symbol: display symbols are neither unique nor stable.
InstrumentId = int


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ValuationStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class MintAuthority:
    """An identity capability for a guarded fact.

    A guarded model accepts only the specific module-level singleton it names.
    This is provenance / accidental-bypass protection, not a security boundary
    against arbitrary code in the same Python process.
    """

    __slots__ = ("_label",)

    def __init__(self, label: str) -> None:
        self._label = label

    def __repr__(self) -> str:
        return f"MintAuthority({self._label!r})"


RISK_AUTHORITY = MintAuthority("risk-and-sizing")
EXECUTION_AUTHORITY = MintAuthority("execution-control")
# Intentionally not re-exported by domain: only the decision-context assembler
# should normally hold this capability.
ASSEMBLER_AUTHORITY = MintAuthority("decision-context-assembler")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentRef(_Frozen):
    """Resolved broker identity plus display and routing metadata."""

    con_id: InstrumentId
    symbol: str
    security_type: str
    exchange: str


@runtime_checkable
class InstrumentResolver(Protocol):
    """Resolves a requested display symbol before it enters the decision pipeline."""

    def resolve(self, symbol: str) -> InstrumentRef: ...


class HoldingValuation(_Frozen):
    """Broker-authoritative valuation state for one signed holding.

    ``UNAVAILABLE`` deliberately has no value or mark timestamp: callers must
    handle the degraded state rather than treating a fabricated zero as value.
    """

    quantity: int
    status: ValuationStatus
    broker_market_value: Decimal | None
    mark_available_at: datetime | None

    @model_validator(mode="after")
    def _validate_status(self) -> HoldingValuation:
        if self.status is ValuationStatus.AVAILABLE:
            if self.broker_market_value is None or self.mark_available_at is None:
                raise ValueError("AVAILABLE valuation requires broker_market_value and mark_available_at")
        elif self.broker_market_value is not None or self.mark_available_at is not None:
            raise ValueError("UNAVAILABLE valuation must not carry a market value or mark timestamp")
        return self


class StrategyIntent(_Frozen):
    """What a strategy proposes — never an order."""

    symbol: str
    target_weight: Decimal
    rationale: str = ""


class RiskPlan(_Frozen):
    """A sized proposal carrying a mandatory stop, produced by Risk Planning."""

    symbol: str
    side: Side
    quantity: int
    stop_price: Decimal
    est_risk_amount: Decimal


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


class _MintGuarded(_Frozen):
    """A frozen fact only a designated issuer may construct via ``_mint``.

    This guard records provenance, not security.  Pydantic's
    ``model_construct()`` bypasses ``__init__``, and arbitrary in-process code
    can obtain public authorities; release-gate containment closes those holes.
    """

    _required_authority: ClassVar[MintAuthority]

    def __init__(self, **data: Any) -> None:
        if data.pop("_authority", None) is not type(self)._required_authority:
            raise TypeError(
                f"{type(self).__name__} cannot be constructed directly; only its designated issuer may "
                f"construct this guarded fact via {type(self).__name__}._mint("
                f"<{type(self)._required_authority._label} authority>, ...)."
            )
        super().__init__(**data)

    @classmethod
    def _mint(cls, authority: MintAuthority, **fields: Any):
        return cls(_authority=authority, **fields)


class RiskContext(_MintGuarded):
    """The assembler-sealed account and market snapshot used for one decision."""

    _required_authority = ASSEMBLER_AUTHORITY

    holdings: Mapping[InstrumentId, HoldingValuation]
    net_liquidation: Decimal
    buying_power: Decimal
    maintenance_margin: Decimal
    prices: Mapping[InstrumentId, Decimal]
    price_basis: Mapping[InstrumentId, str]
    data_as_of: Mapping[InstrumentId, datetime]
    account_observed_at: datetime
    as_of: datetime
    context_digest: str

    def model_post_init(self, __context: Any) -> None:
        """Copy and seal every decision-universe map, including model-construct paths.

        ``frozen=True`` only protects model attributes.  These proxies prevent a
        caller retaining an input dict (or accessing a field) from changing the
        sealed snapshot after minting.  This is separate from the documented
        ``model_construct`` provenance bypass.
        """
        for field_name in ("holdings", "prices", "price_basis", "data_as_of"):
            object.__setattr__(self, field_name, MappingProxyType(dict(getattr(self, field_name))))

    @field_validator("holdings", mode="before")
    @classmethod
    def _reject_duplicate_holding_ids(cls, value: Any) -> dict[InstrumentId, Any]:
        """Accept mappings or pair iterables while rejecting duplicate conIds before dict collapse."""
        pairs = value.items() if isinstance(value, Mapping) else value
        try:
            normalized: dict[InstrumentId, Any] = {}
            for raw_key, holding in pairs:
                key = int(raw_key)
                if key in normalized:
                    raise ValueError(f"duplicate InstrumentId in holdings: {key}")
                normalized[key] = holding
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("duplicate InstrumentId"):
                raise
            raise ValueError(
                "holdings must be a mapping or iterable of (InstrumentId, HoldingValuation)"
            ) from exc
        return normalized

    @field_validator("as_of")
    @classmethod
    def _require_utc_as_of(cls, value: datetime) -> datetime:
        """Reject naive decision instants before causal mark comparisons occur."""
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("context as_of must be UTC")
        return value

    @model_validator(mode="after")
    def _validate_holding_marks(self) -> RiskContext:
        for instrument_id, holding in self.holdings.items():
            if holding.status is not ValuationStatus.AVAILABLE:
                continue
            # HoldingValuation has already established these are present.
            mark_at = holding.mark_available_at
            assert mark_at is not None
            if mark_at.tzinfo is None or mark_at.utcoffset() != UTC.utcoffset(mark_at):
                raise ValueError(f"AVAILABLE holding {instrument_id} mark_available_at must be UTC")
            if mark_at > self.as_of:
                raise ValueError(
                    f"AVAILABLE holding {instrument_id} mark_available_at is after context as_of"
                )
        return self

    @property
    def positions(self) -> dict[InstrumentId, int]:
        """Derived signed inventory view; ``holdings`` is the sole authoritative map."""
        return {instrument_id: holding.quantity for instrument_id, holding in self.holdings.items()}


class ApprovedOrderIntent(_MintGuarded):
    """Risk & Sizing's approval — minted only with ``RISK_AUTHORITY``."""

    _required_authority = RISK_AUTHORITY

    symbol: str
    side: Side
    quantity: int
    stop_price: Decimal
    approved_at: datetime
    ledger_ref: str


class ExecutableOrder(_MintGuarded):
    """The only object an adapter accepts — minted only with ``EXECUTION_AUTHORITY``."""

    _required_authority = EXECUTION_AUTHORITY

    approved: ApprovedOrderIntent
    mode: str
    idempotency_key: str
