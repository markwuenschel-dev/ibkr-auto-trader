"""PT-4a frozen RiskContext contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

import ibkr_trader.domain as domain
from ibkr_trader.domain import (
    HoldingValuation,
    InstrumentRef,
    InstrumentResolver,
    RiskContext,
    ValuationStatus,
)
from ibkr_trader.domain.models import (
    ASSEMBLER_AUTHORITY,
    RISK_AUTHORITY,
    ApprovedOrderIntent,
    Fill,
    RiskPlan,
    Side,
    StrategyIntent,
)

_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)


class _Resolver:
    def resolve(self, symbol: str) -> InstrumentRef:
        return InstrumentRef(
            con_id=265598,
            symbol=symbol,
            security_type="STK",
            exchange="SMART",
        )


def _holding(*, quantity: int = 4) -> HoldingValuation:
    return HoldingValuation(
        quantity=quantity,
        status=ValuationStatus.AVAILABLE,
        broker_market_value=Decimal("800"),
        mark_available_at=_NOW,
    )


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "holdings": {265598: _holding(), 272093: _holding(quantity=-2)},
        "net_liquidation": Decimal("2000"),
        "buying_power": Decimal("4000"),
        "maintenance_margin": Decimal("0"),
        "prices": {265598: Decimal("200")},
        "price_basis": {265598: "BROKER_MARK"},
        "data_as_of": {265598: _NOW},
        "account_observed_at": _NOW,
        "as_of": _NOW,
        "context_digest": "context:abc",
        # The session generation the assembler fenced and sealed (ADR-0002 ⑨).
        "generation": 7,
    }
    fields.update(overrides)
    return fields


def test_instrument_ref_and_resolver_supply_con_id_before_pipeline_entry() -> None:
    resolver = _Resolver()
    assert isinstance(resolver, InstrumentResolver)
    assert resolver.resolve("AAPL") == InstrumentRef(
        con_id=265598,
        symbol="AAPL",
        security_type="STK",
        exchange="SMART",
    )


def test_risk_context_only_designated_assembler_mints() -> None:
    with pytest.raises(TypeError, match="designated issuer"):
        RiskContext(**_fields())  # type: ignore[arg-type]  # negative: no authority
    with pytest.raises(TypeError, match="designated issuer"):
        RiskContext._mint(None, **_fields())  # type: ignore[arg-type]  # negative: wrong authority
    with pytest.raises(TypeError):
        RiskContext._mint(RISK_AUTHORITY, **_fields())

    context = RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields())
    assert context.net_liquidation == Decimal("2000")
    assert not hasattr(domain, "ASSEMBLER_AUTHORITY")
    with pytest.raises(ImportError):
        exec("from ibkr_trader.domain import ASSEMBLER_AUTHORITY", {})


def test_risk_context_seals_all_decision_universe_mappings() -> None:
    context = RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields())

    with pytest.raises(TypeError):
        context.holdings[265598] = _holding(quantity=999)  # type: ignore[index]  # sealed mapping
    with pytest.raises(TypeError):
        del context.prices[265598]  # type: ignore[attr-defined]  # sealed mapping
    with pytest.raises(TypeError):
        context.prices[42] = Decimal("-1")  # type: ignore[index]  # sealed mapping
    with pytest.raises(TypeError):
        context.price_basis[265598] = "QUOTE"  # type: ignore[index]  # sealed mapping
    with pytest.raises(TypeError):
        context.data_as_of[265598] = _NOW + timedelta(seconds=1)  # type: ignore[index]  # sealed

    assert context.positions == {265598: 4, 272093: -2}
    assert context.prices == {265598: Decimal("200")}


def test_positions_are_derived_from_the_single_holdings_map() -> None:
    context = RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields())
    assert context.positions == {265598: 4, 272093: -2}
    assert "positions" not in RiskContext.model_fields
    with pytest.raises(ValidationError):
        RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(positions={265598: 99}))


def test_unavailable_holding_has_no_fabricated_zero() -> None:
    holding = HoldingValuation(
        quantity=1,
        status=ValuationStatus.UNAVAILABLE,
        broker_market_value=None,
        mark_available_at=None,
    )
    assert holding.broker_market_value is None
    with pytest.raises(ValidationError, match="UNAVAILABLE valuation"):
        HoldingValuation(
            quantity=1,
            status=ValuationStatus.UNAVAILABLE,
            broker_market_value=Decimal("0"),
            mark_available_at=None,
        )


def test_available_holding_requires_value_and_causal_mark() -> None:
    with pytest.raises(ValidationError, match="requires broker_market_value"):
        HoldingValuation(
            quantity=1,
            status=ValuationStatus.AVAILABLE,
            broker_market_value=None,
            mark_available_at=_NOW,
        )
    future = HoldingValuation(
        quantity=1,
        status=ValuationStatus.AVAILABLE,
        broker_market_value=Decimal("1"),
        mark_available_at=_NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValidationError, match="after context as_of"):
        RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(holdings={1: future}))


@pytest.mark.parametrize(
    "holdings",
    [
        {1: _holding()},
        {
            1: HoldingValuation(
                quantity=1,
                status=ValuationStatus.UNAVAILABLE,
                broker_market_value=None,
                mark_available_at=None,
            )
        },
    ],
)
def test_naive_as_of_is_rejected_for_available_and_unavailable_holdings(
    holdings: dict[int, HoldingValuation],
) -> None:
    """A naive decision instant must fail cleanly, before any datetime comparison."""
    with pytest.raises(ValidationError, match="context as_of must be UTC"):
        RiskContext._mint(
            ASSEMBLER_AUTHORITY,
            **_fields(holdings=holdings, as_of=_NOW.replace(tzinfo=None)),
        )


def test_duplicate_instrument_ids_are_rejected_before_mapping_collapse() -> None:
    pairs = [(1, _holding()), ("1", _holding(quantity=3))]
    with pytest.raises(ValidationError, match="duplicate InstrumentId"):
        RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(holdings=pairs))


def test_model_construct_bypasses_provenance_guard_release_gate_hole() -> None:
    # Known release-gate hole: model_construct intentionally bypasses __init__.
    bypassed = RiskContext.model_construct(**_fields())  # type: ignore[arg-type]  # negative: bypass
    assert bypassed.context_digest == "context:abc"
    with pytest.raises(TypeError):
        bypassed.holdings[265598] = _holding(quantity=999)  # type: ignore[index]  # sealed mapping


# The invariant ADR-0003 requires and INT-013 enforces: a Python float must never enter a Decimal
# money field. 0.1 + 0.2 (not a bare literal) is the exact binary-imprecision defect — it would
# round-trip to Decimal('0.30000000000000004') under lax coercion.
_FLOAT = 0.1 + 0.2


def _strategy_intent(**over: object) -> dict[str, object]:
    return {"symbol": "AAPL", "target_weight": Decimal("0.3"), **over}


def _fill(**over: object) -> dict[str, object]:
    return {
        "order_id": "o1",
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 10,
        "price": Decimal("1.5"),
        "filled_at": _NOW,
        **over,
    }


def _risk_plan(**over: object) -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 10,
        "stop_price": Decimal("1"),
        "est_risk_amount": Decimal("1"),
        "decision_generation": 1,
        "session_generation": 1,
        **over,
    }


def _holding_fields(**over: object) -> dict[str, object]:
    return {
        "quantity": 4,
        "status": ValuationStatus.AVAILABLE,
        "broker_market_value": Decimal("800"),
        "mark_available_at": _NOW,
        **over,
    }


class TestStrictDecimalBoundary:
    """INT-013: per-field StrictDecimal rejects float ingress into money fields while leaving the
    models' unrelated int/enum/datetime coercions intact (unlike model-level strict)."""

    @pytest.mark.parametrize(
        ("builder", "cls", "field"),
        [
            (_strategy_intent, StrategyIntent, "target_weight"),
            (_fill, Fill, "price"),
            (_risk_plan, RiskPlan, "stop_price"),
            (_risk_plan, RiskPlan, "est_risk_amount"),
            (_holding_fields, HoldingValuation, "broker_market_value"),
        ],
    )
    def test_unguarded_money_field_rejects_float(self, builder, cls, field) -> None:
        cls(**builder())  # sanity: the all-Decimal baseline constructs
        with pytest.raises(ValidationError):
            cls(**builder(**{field: _FLOAT}))

    @pytest.mark.parametrize("field", ["net_liquidation", "buying_power", "maintenance_margin"])
    def test_guarded_riskcontext_scalar_rejects_float_through_mint(self, field) -> None:
        # Guarded models must be tested through their _mint seam so the check proves construction
        # behaviour, not provenance failing first.
        with pytest.raises(ValidationError):
            RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(**{field: _FLOAT}))

    def test_riskcontext_prices_mapping_value_rejects_float(self) -> None:
        # The rule must reach the value type of a mapping, not only scalar fields.
        with pytest.raises(ValidationError):
            RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(prices={265598: _FLOAT}))

    def test_approved_order_intent_stop_price_rejects_float_through_mint(self) -> None:
        with pytest.raises(ValidationError):
            ApprovedOrderIntent._mint(
                RISK_AUTHORITY,
                symbol="AAPL",
                side=Side.BUY,
                quantity=10,
                stop_price=_FLOAT,
                approved_at=_NOW,
                ledger_ref="l1",
            )

    def test_decimal_field_accepts_decimal(self) -> None:
        f = Fill(
            order_id="o1", symbol="AAPL", side=Side.BUY, quantity=10, price=Decimal("0.3"), filled_at=_NOW
        )
        assert f.price == Decimal("0.3")
        ctx = RiskContext._mint(ASSEMBLER_AUTHORITY, **_fields(net_liquidation=Decimal("0.3")))
        assert ctx.net_liquidation == Decimal("0.3")

    def test_optional_decimal_still_accepts_none(self) -> None:
        # UNAVAILABLE holdings carry no value; Decimal | None must still accept None under strict.
        h = HoldingValuation(
            quantity=0,
            status=ValuationStatus.UNAVAILABLE,
            broker_market_value=None,
            mark_available_at=None,
        )
        assert h.broker_market_value is None

    def test_non_decimal_coercions_are_preserved(self) -> None:
        # Per-field strict (not model-level): a str side coerces to the enum and an int quantity is
        # accepted — the ergonomics model-wide strict would have removed.
        f = Fill(
            order_id="o1",
            symbol="AAPL",
            side="BUY",  # type: ignore[arg-type]  # coercion test: str -> Side must still work
            quantity=10,
            price=Decimal("1.5"),
            filled_at=_NOW,
        )
        assert f.side is Side.BUY and f.quantity == 10
