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
from collections.abc import Mapping
from datetime import UTC, datetime

from ib_async import IB  # the quarantined dependency — imported nowhere else

from ..config import Mode
from .config import IbkrConnectionConfig
from .gateway import (
    DISCONNECT_ERROR_CODES,
    ERROR_CODE_CONN_RESTORED,
    Clock,
    _BaseAccountGateway,
)


class IbkrAccountGateway(_BaseAccountGateway):
    """The live gateway: one long-lived, read-only ib_async session per process (decision ②/③)."""

    def __init__(
        self,
        *,
        config: IbkrConnectionConfig,
        mode: Mode,
        clock: Clock | None = None,
        store: object | None = None,
        emitter: object | None = None,
        ib: IB | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            config=config, mode=mode, clock=clock, store=store, emitter=emitter, **kwargs  # type: ignore[arg-type]
        )
        self._ib = ib or IB()
        self._events_wired = False
        # Keep strong refs to reconnect tasks so the loop cannot GC them mid-flight (RUF006).
        self._bg_tasks: set[asyncio.Task] = set()

    # ---- raw connection --------------------------------------------------- #
    async def _raw_connect(self) -> None:
        cfg = self._config
        assert isinstance(cfg, IbkrConnectionConfig)
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
        self._events_wired = True

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
        rows = await self._ib.reqAccountSummaryAsync()
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

    async def _fetch_positions(self) -> Mapping[str, object]:
        positions: dict[str, object] = {}
        for pos in self._ib.positions(self._account):
            symbol = pos.contract.symbol
            # Signed shares aggregate across any duplicate rows for the same symbol.
            positions[symbol] = int(positions.get(symbol, 0)) + int(pos.position)
        return positions

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
