"""ibkr — connection/session (ib_async, asyncio), account snapshot -> RiskContext, market data (PT-3/PT-4).

Also home of resilience: reconnection, rate-limiting, and the heartbeat/watchdog that pauses safely on
stall or disconnect (§6.3). Empty until PT-3.
"""
