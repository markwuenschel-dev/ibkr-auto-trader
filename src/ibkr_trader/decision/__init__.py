"""decision — the sealed decision-context assembly seam (PT-4c).

Home of the ``DecisionContextAssembler``: the *sole* production constructor of ``RiskContext`` (ADR-0002
④/⑤). It acquires the account snapshot then the quote batch, seals ``decision_at`` at the close of
collection, runs the pure causal gate, honours the reconnect generation fence, and mints the sealed,
conId-keyed ``RiskContext`` via ``ASSEMBLER_AUTHORITY``. ib_async-free.
"""

from __future__ import annotations

from .assembler import (
    DecisionContextAssembler,
    DecisionTimeSource,
    GenerationFenceError,
    SystemDecisionClock,
)

__all__ = [
    "DecisionContextAssembler",
    "DecisionTimeSource",
    "GenerationFenceError",
    "SystemDecisionClock",
]
