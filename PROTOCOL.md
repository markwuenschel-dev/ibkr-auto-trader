# PROTOCOL.md — ibkr-auto-trader

**Project**: Automated Trading System for IBKR Taxable Account  
**Goal**: Build a safe, auditable, production-grade automated trading system that can grow the ~$2k taxable account over time while strictly respecting risk, taxes, and platform constraints.  
**Philosophy**: Treat all agent-generated trading code and logic as untrusted. Verification (human + independent reviewer + adversarial probes) is mandatory before any live execution.

## Core Rules (Non-Negotiable)
1. **Paper Trading First, Always**  
   - All development, backtesting, and initial validation happens exclusively in IBKR Paper Trading account.  
   - Live trading is only permitted after:  
     - Comprehensive backtesting with walk-forward validation  
     - Independent reviewer sign-off  
     - Adversarial regression hunt passed  
     - Small-position live test with human monitoring  
   - Any code that touches live account must have explicit "LIVE_TRADING_ENABLED = False" guard by default.

2. **Risk Management is Sacred**  
   - Maximum risk per trade: 1% of current portfolio equity (adjustable only via explicit reviewed change).  
   - Daily loss limit: 3% of equity — system must pause trading if breached.  
   - Position sizing must always respect current Net Liquidation Value, Buying Power, and Maintenance Margin.  
   - No leverage beyond conservative levels (target < 1.5x).  
   - Every order must include stop-loss or equivalent risk control unless explicitly reviewed and approved for a specific strategy.

3. **No Lookahead Bias or Data Leakage**  
   - All signals, features, and decisions must be strictly causal (based only on data available at decision time).  
   - Walk-forward / purged K-fold validation required for any ML or rule-based strategy.  
   - Timestamp handling must be explicit and audited.

4. **Tax & Regulatory Awareness (Taxable Account)**  
   - Prefer strategies that minimize short-term capital gains where possible.  
   - Track wash-sale implications if loss harvesting is used.  
   - Full decision audit log (why a trade was taken, expected edge, risk parameters) must be written for every action.  
   - Never trade in ways that could be interpreted as manipulative or against IBKR terms.

5. **Auditability & Observability**  
   - Every trading decision, order, fill, and P&L impact must be logged with full context (timestamp, market state, rationale, risk metrics).  
   - System must expose clear metrics: Sharpe-like ratios (with proper methodology), max drawdown, win rate, expectancy, turnover.  
   - Human-readable explanations for every automated action.

6. **Error Handling & Resilience**  
   - Robust reconnection logic for IBKR API.  
   - Graceful degradation: on any error, system must pause, notify, and never leave open risky positions.  
   - Rate limiting and API usage respect built-in.

7. **Verification Process**  
   - Builder proposes changes via handoff.  
   - Independent reviewer (separate agent session) must review the actual diff + context.  
   - For any money/risk-related change: run `diff-regression-hunt` adversarial workflow in parallel.  
   - Human (you) has final veto via Telegram or direct file edit.  
   - Only after all three lenses agree does code move toward live testing.

## Current Holdings Context (as of July 2026)
- ~$2,033 Net Liquidation Value  
- Positions: QQQ (~30%), SPY (~29%), VXUS (~19%), DFSI (~11%), Cash (~11%)  
- Ultra-aggressive equity tilt consistent with your Roth IRA philosophy, but in taxable account.  
- Goal: Grow this sleeve responsibly while experimenting with quant ideas.

## Technology Stack
- Python + ib_insync (or ib_async) for IBKR TWS/Gateway API  
- Paper trading port 7497 during development  
- Local execution on your RTX 5090 machine (or secure VPS later)  
- Full test coverage (pytest), type checking (mypy), linting  
- Structured logging + decision audit trail

## Success Criteria
- System can safely run 24/7 in paper with positive expectancy.  
- Clear, reviewed path to small live deployment.  
- You understand every rule and can override/disable instantly.  
- No silent failures or hidden risk accumulation.

This protocol is living — changes only via reviewed handoff + your approval.
