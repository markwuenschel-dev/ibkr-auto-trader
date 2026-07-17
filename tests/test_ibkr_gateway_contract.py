"""CI drift-guard for the ``ib_async`` API surface the live ``IbkrAccountGateway`` depends on.

``ibkr_gateway._fetch_*`` translate live ``ib_async`` objects into the dicts / ``HeldPosition``s the base
consumes. That module is normally exercised **only** by the opt-in ``IBKR_INTEGRATION`` test against a real
paper Gateway (``verify.py`` runs ``-m "not integration"``), so a method-name or object-shape drift in
``ib_async`` would pass CI silently until a live run — the exact risk the module docstring flags ("validate
[the method names] against the installed ib_async").

This pins the surface **without a socket**, in two complementary halves:

* ``TestIbMethodSurface`` — introspects the real ``ib_async.IB`` for the method names ``_fetch_*`` call.
  A fake IB (below) cannot catch this: it would define those names itself. Only the real class can.
* ``TestFetchTranslation`` — runs the real ``_fetch_*`` over real ``ib_async`` value objects behind a fake
  IB, so a renamed *field* on ``AccountValue`` / ``PortfolioItem`` / ``Position`` / ``Contract`` goes red.

No network; runs in CI (``ib_async`` is a core dependency). It does NOT replace the manual integration
test's live-connection coverage — it guards the API *surface* (names + shapes).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import ib_async
from ib_async import IB

from ibkr_trader.config import Mode
from ibkr_trader.domain import ValuationStatus
from ibkr_trader.ibkr import FixedClock, IbkrConnectionConfig
from ibkr_trader.ibkr.ibkr_gateway import IbkrAccountGateway

_MOMENT = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)
_ACCT = "DU111"
_AAPL = 265598


def _run(coro):
    return asyncio.run(coro)


def _acct_value(tag: str, value: str) -> ib_async.AccountValue:
    return ib_async.AccountValue(account=_ACCT, tag=tag, value=value, currency="USD", modelCode="")


class _FakeIB:
    """A stand-in for a connected ``ib_async.IB``: returns pre-built REAL ib_async value objects. Its method
    NAMES are ours, so it does not prove ib_async's names — that is ``TestIbMethodSurface``'s job."""

    def __init__(self, *, summary=(), portfolio=(), positions=()):
        self._summary = list(summary)
        self._portfolio = list(portfolio)
        self._positions = list(positions)

    async def reqAccountSummaryAsync(self):
        return self._summary

    async def reqAccountUpdatesAsync(self, _subscribe, _account):
        return None

    def portfolio(self, _account=""):
        return self._portfolio

    def positions(self, _account=""):
        return self._positions


def _gateway(fake: _FakeIB) -> IbkrAccountGateway:
    gw = IbkrAccountGateway(
        config=IbkrConnectionConfig(),
        mode=Mode.PAPER,
        clock=FixedClock(_MOMENT),
        # A duck-typed fake instead of a real IB() so nothing connects; it implements only the methods
        # _fetch_* call. The parameter is typed IB | None, hence the scoped ignore.
        ib=fake,  # type: ignore[arg-type]
    )
    gw._account = _ACCT  # resolved account is normally set at connect; set it directly for the unit
    return gw


class TestIbMethodSurface:
    """(c) — the method names ``_fetch_*`` call must exist on the installed ``ib_async.IB``."""

    def test_ib_exposes_every_method_fetch_star_calls(self):
        for name in (
            "reqAccountSummaryAsync",  # _fetch_summary
            "portfolio",  # _fetch_held_positions (valuation)
            "positions",  # _fetch_held_positions (verification)
            "reqAccountUpdatesAsync",  # _warm_account_updates
            "reqCurrentTimeAsync",  # _fetch_broker_time
        ):
            assert hasattr(IB, name), (
                f"ib_async.IB lost {name!r}: IbkrAccountGateway._fetch_* will AttributeError against a live "
                "Gateway. Update the adapter (and this guard) to the new ib_async spelling."
            )

    def test_ib_exposes_an_accounts_getter(self):
        # _fetch_accounts accepts getAccounts() OR managedAccounts() — at least one must exist.
        assert hasattr(IB, "getAccounts") or hasattr(IB, "managedAccounts")


class TestFetchTranslation:
    """(b) — the real ``_fetch_*`` must keep reading the fields it reads off real ib_async objects."""

    def test_fetch_summary_reads_accountvalue_account_tag_value(self):
        rows = [
            _acct_value("NetLiquidation", "2000.50"),
            _acct_value("BuyingPower", "8000.00"),
            # A blank value for the resolved account must be dropped (not overwrite a real tag).
            _acct_value("MaintMarginReq", ""),
        ]
        out = _run(_gateway(_FakeIB(summary=rows))._fetch_summary())
        assert out == {"NetLiquidation": "2000.50", "BuyingPower": "8000.00"}

    def test_fetch_held_positions_reads_portfolioitem_and_contract_fields(self):
        contract = ib_async.Contract(conId=_AAPL, symbol="AAPL")
        portfolio = [
            ib_async.PortfolioItem(
                contract=contract,
                position=10.0,
                marketPrice=150.0,
                marketValue=1500.0,
                averageCost=140.0,
                unrealizedPNL=100.0,
                realizedPNL=0.0,
                account=_ACCT,
            )
        ]
        positions = [ib_async.Position(account=_ACCT, contract=contract, position=10.0, avgCost=140.0)]
        gw = _gateway(_FakeIB(portfolio=portfolio, positions=positions))
        # Seed the updatePortfolio receipt so the holding is AVAILABLE and its broker_market_value is kept —
        # otherwise a renamed `marketValue` would silently degrade to UNAVAILABLE and hide the drift.
        gw._portfolio_mark_at = {_AAPL: _MOMENT}

        held = list(_run(gw._fetch_held_positions()))

        assert len(held) == 1
        h = held[0]
        assert h.instrument_id == _AAPL  # Contract.conId
        assert h.symbol == "AAPL"  # Contract.symbol
        assert h.quantity == 10  # PortfolioItem.position (signed int)
        assert h.valuation_status is ValuationStatus.AVAILABLE
        assert h.broker_market_value == Decimal("1500.0")  # PortfolioItem.marketValue read through

    def test_fetch_held_positions_fails_closed_when_portfolio_and_positions_disagree(self):
        contract = ib_async.Contract(conId=_AAPL, symbol="AAPL")
        portfolio = [
            ib_async.PortfolioItem(
                contract=contract, position=10.0, marketPrice=150.0, marketValue=1500.0,
                averageCost=140.0, unrealizedPNL=0.0, realizedPNL=0.0, account=_ACCT,
            )
        ]
        # positions() disagrees on quantity — the fail-closed reconcile must raise, not value a book we
        # cannot verify. Pins that .contract.conId / .position are read off BOTH object kinds.
        positions = [ib_async.Position(account=_ACCT, contract=contract, position=7.0, avgCost=140.0)]
        gw = _gateway(_FakeIB(portfolio=portfolio, positions=positions))
        from ibkr_trader.ibkr import SnapshotIncomplete

        try:
            _run(gw._fetch_held_positions())
            raise AssertionError("expected SnapshotIncomplete on a portfolio/positions mismatch")
        except SnapshotIncomplete:
            pass
