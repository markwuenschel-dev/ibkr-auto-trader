"""risk — the Risk & Sizing deep module (PT-5/PT-6/PT-7): Rules Ledger + Planner + Approver.

Sizes StrategyIntents into RiskPlans (with stops), evaluates them against the Rules Ledger (1%/trade,
-3% daily lockout, buying-power, margin, leverage<1.5x, stop-required, causal-only, idempotency), and —
only on approval — mints an ApprovedOrderIntent. Approvals AND rejections are logged. Empty until PT-5.
"""
