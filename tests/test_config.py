"""RiskPolicy control-plane contract tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibkr_trader.config import RiskPolicy, Settings

_DECIMAL_LIMIT_FIELDS = (
    "max_risk_per_trade",
    "daily_realized_lockout_pct",
    "session_drawdown_pct",
    "leverage_cap",
)


def test_default_risk_policy_is_versioned_decimal_and_frozen() -> None:
    policy = RiskPolicy()

    assert policy.version == "v1"
    assert policy.max_risk_per_trade == Decimal("0.01")
    assert policy.daily_realized_lockout_pct == Decimal("0.03")
    assert policy.session_drawdown_pct == Decimal("0.10")
    assert policy.leverage_cap == Decimal("1.5")
    assert policy.stop_loss_required is True
    assert all(isinstance(getattr(policy, field), Decimal) for field in _DECIMAL_LIMIT_FIELDS)
    with pytest.raises(ValidationError):
        policy.leverage_cap = Decimal("2")  # type: ignore[misc]


def test_threshold_boundaries_use_decimal_and_drawdown_is_independent() -> None:
    policy = RiskPolicy()
    session_start_equity = Decimal("1000.00")

    realized_lockout_boundary = -policy.daily_realized_lockout_pct * session_start_equity
    drawdown_boundary = -policy.session_drawdown_pct * session_start_equity

    assert realized_lockout_boundary == Decimal("-30.0000")
    assert drawdown_boundary == Decimal("-100.0000")
    assert policy.session_drawdown_pct != policy.daily_realized_lockout_pct


def test_settings_uses_a_fresh_default_risk_policy() -> None:
    first = Settings()
    second = Settings()

    assert isinstance(first.risk, RiskPolicy)
    assert first.risk is not second.risk


# --------------------------------------------------------------------------- #
# float ingress — ADR-0003: limits convert float -> Decimal BEFORE they
# participate in any money calculation; "no float in money arithmetic".
# Decimal annotations alone did not enforce this: lax mode coerced a float via
# str(), preserving binary imprecision inside a reviewed money limit.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", _DECIMAL_LIMIT_FIELDS)
def test_a_float_limit_is_rejected_not_coerced(field: str) -> None:
    with pytest.raises(ValidationError):
        RiskPolicy(**{field: 0.1})  # type: ignore[arg-type]  # the float is the point


def test_an_imprecise_float_cannot_enter_a_limit() -> None:
    """The reproduction: 0.1 + 0.2 is not 0.3, and str()-coercion preserved that exactly."""
    with pytest.raises(ValidationError):
        RiskPolicy(session_drawdown_pct=0.1 + 0.2)  # type: ignore[arg-type]


def test_control_2_fires_at_exactly_the_drawdown_boundary() -> None:
    """A session exactly 30% down must trip a 30% limit.

    With float ingress, session_drawdown_pct held Decimal("0.30000000000000004"); the boundary
    landed just past -300.00 and the loss control did not fire.
    """
    policy = RiskPolicy(session_drawdown_pct=Decimal("0.30"))
    realized, session_start_equity = Decimal("-300.00"), Decimal("1000.00")

    assert realized <= -policy.session_drawdown_pct * session_start_equity


def test_stop_loss_required_rejects_a_falsy_int() -> None:
    """The mandatory-stop flag must not be silently disabled by 0."""
    with pytest.raises(ValidationError):
        RiskPolicy(stop_loss_required=0)  # type: ignore[arg-type]


def test_stop_loss_required_rejects_a_truthy_int() -> None:
    with pytest.raises(ValidationError):
        RiskPolicy(stop_loss_required=1)  # type: ignore[arg-type]


def test_decimal_limits_are_still_accepted() -> None:
    policy = RiskPolicy(session_drawdown_pct=Decimal("0.30"), stop_loss_required=False)

    assert policy.session_drawdown_pct == Decimal("0.30")
    assert policy.stop_loss_required is False


def test_defaults_still_construct_and_stay_decimal() -> None:
    """Strict must not break the reviewed defaults — they are the values that ship."""
    policy = RiskPolicy()

    assert policy.session_drawdown_pct == Decimal("0.10")
    assert all(isinstance(getattr(policy, f), Decimal) for f in _DECIMAL_LIMIT_FIELDS)
    assert policy.stop_loss_required is True
    assert Settings().risk.session_drawdown_pct == Decimal("0.10")


# --------------------------------------------------------------------------- #
# (de)serialization — what this repository ACTUALLY supports.
#
# No call site in src/ or tests/ constructs a RiskPolicy from JSON or dumps one:
# `RiskPolicy(...)` appears only in config.py's own default and in tests, and a
# search of src/+tests/ for model_validate/model_dump/parse_raw found nothing.
# These pin the boundary anyway, because "there is no loader today" is a fact
# about today, and the next person to add one needs the contract already nailed
# down rather than discovered by a mis-set limit in production.
# --------------------------------------------------------------------------- #


def test_a_dumped_policy_round_trips_exactly() -> None:
    policy = RiskPolicy(session_drawdown_pct=Decimal("0.30"))

    restored = RiskPolicy.model_validate_json(policy.model_dump_json())

    assert restored == policy
    assert restored.session_drawdown_pct == Decimal("0.30")


def test_decimals_serialize_as_strings_not_json_floats() -> None:
    """The dump must not go through a binary float — that would defeat the whole point."""
    dumped = RiskPolicy(session_drawdown_pct=Decimal("0.30")).model_dump_json()

    assert '"session_drawdown_pct":"0.30"' in dumped
    assert '"stop_loss_required":true' in dumped
    assert isinstance(RiskPolicy().model_dump()["session_drawdown_pct"], Decimal)


def test_python_mode_validation_rejects_a_float_like_the_constructor_does() -> None:
    """A dict from a stdlib loader is the realistic ingress shape, and strict refuses it.

    ``json.loads`` yields Python ``float``s, so ``RiskPolicy(**json.loads(...))`` was the path by which
    an imprecise limit could have entered. It is now rejected rather than coerced.
    """
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate({"session_drawdown_pct": 0.1 + 0.2})

    assert RiskPolicy.model_validate(
        {"session_drawdown_pct": Decimal("0.30")}
    ).session_drawdown_pct == Decimal("0.30")


def test_json_numbers_are_parsed_from_text_and_never_via_a_binary_float() -> None:
    """Strict does NOT reject a JSON number — and does not need to.

    JSON has no Decimal type, so pydantic's strict JSON mode still accepts a number here. That is safe,
    and worth pinning so nobody "fixes" it: pydantic-core reads the literal TEXT into the Decimal, so
    ``0.3`` yields ``Decimal("0.3")`` exactly — a float round-trip (which is what produced
    ``0.30000000000000004``) never happens on this path. What you wrote is what you get.
    """
    for literal in ("0.3", "0.30", "0.1", "0.30000000000000004"):
        parsed = RiskPolicy.model_validate_json(f'{{"session_drawdown_pct": {literal}}}').session_drawdown_pct
        assert parsed == Decimal(literal), f"{literal} must survive as its own exact value"

    # the flag stays strict on the JSON path too: an int is not a bool
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate_json('{"stop_loss_required": 0}')
    assert RiskPolicy.model_validate_json('{"stop_loss_required": true}').stop_loss_required is True
