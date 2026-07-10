"""fake_gateway — the in-memory, deterministic ``AccountGateway`` used by tests (decision ①).

No socket, no ib_async, no wall-clock. It feeds hand-set account-summary / positions / accounts dicts
through the *same* ``_BaseAccountGateway`` orchestration the live adapter uses, so the CI test surface
exercises the real mapping / resolution / reconciliation / reconnect logic — only the ``_fetch_*`` hooks
differ. Test knobs (``fail_next``, ``simulate_drop``, ``simulate_reconnect``) let a test drive backoff and
the reactive drop path with zero wall-clock (inject a no-op ``sleep``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ..config import Mode
from .config import IbkrConnectionConfig
from .gateway import Clock, _BaseAccountGateway


async def _noop_sleep(_seconds: float) -> None:
    """Default sleep for the fake: instant, so backoff loops cost no wall-clock in tests."""
    return None


class FakeAccountGateway(_BaseAccountGateway):
    """A deterministic ``AccountGateway`` backed by in-memory dicts.

    Set ``summary`` (tag -> string value), ``positions`` (symbol -> signed int), ``accounts`` (what
    ``getAccounts()`` would return) and, optionally, ``broker_time`` (to exercise clock-skew). The gateway
    resolves/maps/reconciles exactly as the live one does.
    """

    def __init__(
        self,
        *,
        config: IbkrConnectionConfig | None = None,
        mode: Mode = Mode.PAPER,
        clock: Clock | None = None,
        store: object | None = None,
        emitter: object | None = None,
        accounts: list[str] | None = None,
        summary: Mapping[str, object] | None = None,
        positions: Mapping[str, object] | None = None,
        broker_time: datetime | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            config=config or IbkrConnectionConfig(),
            mode=mode,
            clock=clock,
            store=store,
            emitter=emitter,
            sleep=kwargs.pop("sleep", None) or _noop_sleep,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )
        # ``getAccounts()`` default: a single DU paper account, so the happy path needs no wiring.
        self.accounts: list[str] = accounts if accounts is not None else ["DU1234567"]
        self.summary: dict[str, object] = dict(summary or {})
        self.positions: dict[str, object] = dict(positions or {})
        self.broker_time: datetime | None = broker_time
        #: How many upcoming ``_raw_connect`` calls should fail (models a flaky socket for backoff tests).
        self._pending_connect_failures = 0
        #: Count of raw connect attempts — a test can assert backoff actually retried.
        self.connect_attempts = 0

    # ---- test knobs ------------------------------------------------------- #
    def fail_next(self, n: int) -> None:
        """Schedule the next ``n`` connect attempts to fail (then succeed). Drives bounded-backoff."""
        self._pending_connect_failures += n

    def simulate_drop(self) -> None:
        """Model an observed connection drop with no auto-reconnect: health goes False (a test then
        drives ``simulate_reconnect`` to watch it recover)."""
        self._connected = False

    async def simulate_reconnect(self) -> None:
        """Run the reactive bounded-backoff reconnect (as the real ``disconnectedEvent`` path would)."""
        await self.reconnect()

    async def simulate_disconnect_event(self) -> None:
        """Fire the full reactive handler: drop -> emit ``ibkr.disconnect`` -> backoff reconnect."""
        await self._on_disconnect()

    # ---- base hooks ------------------------------------------------------- #
    async def _raw_connect(self) -> None:
        self.connect_attempts += 1
        if self._pending_connect_failures > 0:
            self._pending_connect_failures -= 1
            raise ConnectionError("fake gateway: simulated transient connect failure")

    async def _raw_disconnect(self) -> None:
        return None

    async def _fetch_accounts(self) -> list[str]:
        return list(self.accounts)

    async def _fetch_summary(self) -> Mapping[str, object]:
        return dict(self.summary)

    async def _fetch_positions(self) -> Mapping[str, object]:
        return dict(self.positions)

    async def _fetch_broker_time(self) -> datetime | None:
        return self.broker_time
