"""domain — frozen pydantic models + the type-gated constructibility seams (PT-1).

Home of RiskContext, StrategyIntent, RiskPlan, ApprovedOrderIntent, ExecutableOrder, Fill/Ack. The
safety seam lives here: ApprovedOrderIntent/ExecutableOrder have no strategy-usable constructor, so a
strategy can never mint an executable order (§2 constructibility rule).
"""

from .models import (
    EXECUTION_AUTHORITY,
    RISK_AUTHORITY,
    Ack,
    ApprovedOrderIntent,
    ExecutableOrder,
    Fill,
    MintAuthority,
    RiskContext,
    RiskPlan,
    Side,
    StrategyIntent,
)

__all__ = [
    "EXECUTION_AUTHORITY",
    "RISK_AUTHORITY",
    "Ack",
    "ApprovedOrderIntent",
    "ExecutableOrder",
    "Fill",
    "MintAuthority",
    "RiskContext",
    "RiskPlan",
    "Side",
    "StrategyIntent",
]
