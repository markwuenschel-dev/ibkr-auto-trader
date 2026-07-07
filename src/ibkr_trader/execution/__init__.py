"""execution — Execution Control deep module (PT-8/PT-9/PT-10): ModeController + ExecutionGate + adapters.

Authorizes an ApprovedOrderIntent in the current mode, mints the ExecutableOrder, and routes it to an
adapter. Adapters accept ONLY ExecutableOrder (bypass is type-impossible): `simulated` (default) and
`paper_ibkr` now; `live_ibkr` is FUTURE-ONLY and mode-gated. Kill/pause block submission. Empty until PT-8.
"""
