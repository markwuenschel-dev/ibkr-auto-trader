"""ibkr_gateway — the real ``AccountGateway``, backed by ib_async. THE ONLY MODULE THAT IMPORTS ib_async.

This is the containment boundary (decision ①): every ib_async call in the codebase lives here. Everything
load-bearing — mapping the summary to ``RiskContext``, resolving the account, reconciling, backoff — is
inherited from ``_BaseAccountGateway``; this file only supplies the ``_raw_*``/``_fetch_*`` hooks that turn
ib_async objects into the plain dicts the base consumes, plus the reactive event wiring (decision ⑥).

Because this module imports ib_async, it is **not** imported in CI (the fake is). It is exercised only by
the opt-in, manually-run integration test against a real paper Gateway. The ib_async method names below
follow ADR 0001; validate them against the installed ib_async when running that manual test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ib_async import IB  # the quarantined dependency — imported nowhere else

from ..config import Mode
from ..telemetry import TelemetrySink
from .config import IbkrConnectionConfig
from .gateway import (
    DISCONNECT_ERROR_CODES,
    ERROR_CODE_CONN_RESTORED,
    Clock,
    HeldPosition,
    SnapshotIncomplete,
    _BaseAccountGateway,
    _PositionStore,
)
from .session import (
    STAGE_PORTFOLIO_RECONCILE,
    Session,
    aggregate_signed,
    reconcile_inventory,
)


def _to_decimal(raw: object) -> Decimal | None:
    """Parse a broker numeric to ``Decimal`` via ``str`` (never ``float``), or ``None`` if unparseable."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


class IbkrAccountGateway(_BaseAccountGateway):
    """The live gateway: one long-lived, read-only ib_async session per process (decision ②/③)."""

    def __init__(
        self,
        *,
        config: IbkrConnectionConfig,
        mode: Mode,
        clock: Clock | None = None,
        store: _PositionStore | None = None,
        emitter: TelemetrySink | None = None,
        session: Session | None = None,
        ib: IB | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            config=config,
            mode=mode,
            clock=clock,
            store=store,
            emitter=emitter,
            session=session,
            **kwargs,  # type: ignore[arg-type]
        )
        self._ib = ib or IB()
        self._events_wired = False
        # Keep strong refs to reconnect tasks so the loop cannot GC them mid-flight (RUF006).
        self._bg_tasks: set[asyncio.Task] = set()
        # ADR-0002 ⑫: receipt time of each ``updatePortfolio`` event per conId — this, not the account
        # read time, is a holding's ``mark_available_at``. A conId absent here is UNAVAILABLE (never a
        # fabricated mark). Reset on reconnect so a stale pre-drop mark can never carry a new generation.
        self._portfolio_mark_at: dict[int, datetime] = {}
        self._account_updates_warm = False

    # ---- raw connection --------------------------------------------------- #
    async def _raw_connect(self) -> None:
        cfg = self._config
        assert isinstance(cfg, IbkrConnectionConfig)
        # A (re)connect starts a fresh generation of broker state: drop stale portfolio marks and force a
        # re-warm so no pre-drop mark is ever welded to post-reconnect inventory (ADR-0002 ⑨/⑫).
        self._portfolio_mark_at.clear()
        self._account_updates_warm = False
        await self._ib.connectAsync(
            host=cfg.host,
            port=cfg.port,
            clientId=cfg.client_id,
            timeout=cfg.connect_timeout,
            readonly=cfg.readonly,  # decision ③: read-only, always
            account=cfg.account or "",
        )
        self._wire_events()

    async def _raw_disconnect(self) -> None:
        self._ib.disconnect()

    def _wire_events(self) -> None:
        """Subscribe once to the drop signals PT-3 reacts to (decision ⑥). ib_async fires these
        synchronously on the loop, so we schedule the async reconnect with ``ensure_future``."""
        if self._events_wired:
            return
        self._ib.disconnectedEvent += self._on_ib_disconnect
        self._ib.errorEvent += self._on_ib_error
        # ADR-0002 ⑫: stamp the receipt time of each portfolio update so a holding's mark carries real
        # availability provenance (not the account read time). Guarded — older ib_async may lack the event.
        portfolio_event = getattr(self._ib, "updatePortfolioEvent", None)
        if portfolio_event is not None:
            portfolio_event += self._on_update_portfolio
        self._events_wired = True

    def _on_update_portfolio(self, item: object, *_args: object) -> None:
        contract = getattr(item, "contract", None)
        con_id = getattr(contract, "conId", None)
        if con_id is not None:
            self._portfolio_mark_at[int(con_id)] = self._clock.now()

    def _on_ib_disconnect(self, *_args: object) -> None:
        self._schedule(self._on_disconnect())

    def _on_ib_error(
        self, _req_id: int, error_code: int, _error_string: str, _contract: object = None
    ) -> None:
        if error_code == ERROR_CODE_CONN_RESTORED:
            # 1102: connectivity restored with data maintained — health recovers without a reconnect.
            self._connected = True
            return
        if error_code in DISCONNECT_ERROR_CODES:  # 1100 lost / 1300 socket reset -> reconnect
            self._schedule(self._on_disconnect())

    def _schedule(self, coro: object) -> None:
        try:
            task = asyncio.ensure_future(coro)  # type: ignore[arg-type]
        except RuntimeError:  # no running loop (e.g. during teardown) — nothing to react on
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ---- warm-state reads ------------------------------------------------- #
    async def _fetch_accounts(self) -> list[str]:
        # ADR names getAccounts(); the installed ib_async exposes managedAccounts(). Accept either so the
        # manual integration run does not AttributeError on a naming mismatch.
        getter = getattr(self._ib, "getAccounts", None) or getattr(self._ib, "managedAccounts", None)
        if getter is None:
            return []
        return [a for a in (getter() or []) if a]

    async def _fetch_summary(self) -> Mapping[str, object]:
        rows = await self._ib.reqAccountSummaryAsync() or []
        acct = self._account
        summary: dict[str, object] = {}
        for row in rows:
            row_acct = getattr(row, "account", None)
            # Keep rows for the resolved account (or account-agnostic rows). Non-empty values win over a
            # stray blank for the same tag.
            if acct and row_acct not in (acct, None, "", "All"):
                continue
            value = getattr(row, "value", None)
            if value in (None, ""):
                continue
            summary[row.tag] = value
        return summary

    async def _warm_account_updates(self) -> None:
        """Warm + verify the account-update subscription so ``ib.portfolio()`` is populated (ADR-0002 ⑫).

        Held valuation is *account-sourced*, not a blind ``positions()``→``portfolio()`` swap: without a
        warm subscription the portfolio is empty and every holding degrades to UNAVAILABLE. Guarded on the
        method name (older ib_async spellings) — best-effort; a failure leaves marks absent, not fabricated.
        """
        if self._account_updates_warm:
            return
        warm = getattr(self._ib, "reqAccountUpdatesAsync", None)
        if warm is not None:
            try:
                await warm(True, self._account or "")
            except Exception:  # a warm failure degrades valuation to UNAVAILABLE, never fabricates it
                return
        self._account_updates_warm = True

    async def _fetch_held_positions(self) -> Iterable[HeldPosition]:
        """Held inventory from ``ib.portfolio()`` (valuation) verified against ``ib.positions()``.

        Fail-closed: if portfolio inventory does not reconcile with position inventory (by conId, non-zero
        quantities), the account-update subscription is not trustworthy for valuation — raise
        ``SnapshotIncomplete`` rather than value a book we cannot verify (ADR-0002 ⑫). A holding with no
        recorded ``updatePortfolio`` receipt time is surfaced UNAVAILABLE, never fabricated.
        """
        await self._warm_account_updates()
        account = self._account or ""
        portfolio_items = [
            item for item in self._ib.portfolio(account) if int(item.position) != 0
        ]
        portfolio_qty = aggregate_signed(
            (item.contract.conId, int(item.position)) for item in portfolio_items
        )
        position_qty = aggregate_signed(
            (pos.contract.conId, int(pos.position)) for pos in self._ib.positions(account)
        )
        ok, mismatched = reconcile_inventory(portfolio_qty, position_qty)
        self._emit(
            STAGE_PORTFOLIO_RECONCILE,
            metrics={"reconciled": ok, "mismatched_instruments": len(mismatched)},
        )
        if not ok:
            raise SnapshotIncomplete(
                f"portfolio inventory does not reconcile with positions for conIds {mismatched}"
            )
        held: list[HeldPosition] = []
        for item in portfolio_items:
            con_id = int(item.contract.conId)
            held.append(
                HeldPosition.from_broker(
                    instrument_id=con_id,
                    symbol=item.contract.symbol,
                    quantity=int(item.position),
                    market_value=_to_decimal(getattr(item, "marketValue", None)),
                    market_price=_to_decimal(getattr(item, "marketPrice", None)),
                    mark_available_at=self._portfolio_mark_at.get(con_id),
                )
            )
        return held

    async def _fetch_broker_time(self) -> datetime | None:
        try:
            broker_time = await self._ib.reqCurrentTimeAsync()
        except Exception:  # skew monitoring is best-effort; never fail a snapshot over it
            return None
        if broker_time is None:
            return None
        if broker_time.tzinfo is None:
            broker_time = broker_time.replace(tzinfo=UTC)
        return broker_time.astimezone(UTC)
