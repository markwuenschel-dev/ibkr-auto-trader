"""gateway — the AccountGateway *port*, its error model, and the ib_async-free machinery behind it (PT-3).

This module is the containment boundary's *inside*: everything PT-3 needs that is **not** ib_async and
**not** the fake lives here, so both the real adapter (``ibkr_gateway.py``) and the fake
(``fake_gateway.py``) share one implementation of the load-bearing logic — mapping a broker summary to a
frozen ``RiskContext``, resolving the account, reconciling against the PT-2 cache, and driving reactive
reconnection. That sharing is deliberate: the CI-safe fake tests exercise the *same* code the live
adapter runs; only the socket plumbing (the ``_fetch_*`` / ``_raw_*`` hooks) differs.

Design is fixed by ADR 0001 (decisions 1-9). The rules that show up as code below:

  * ``snapshot()`` never returns a partial or zero-filled ``RiskContext`` — a missing/garbled field
    raises ``SnapshotIncomplete`` (decision ⑦).
  * Money parses straight from the broker's summary strings to ``Decimal``, never via ``float``
    (decision ⑧). Positions are signed ``int`` by symbol.
  * Account resolution fails closed on ambiguity, and under ``Mode.PAPER`` asserts a ``DU``-prefixed
    paper account (decision ④). Those are *fatal* errors raised at ``connect()``.
  * Reconciliation *reports*, it does not *gate* (decision ⑤): broker is truth, divergence emits
    ``positions.reconcile`` + rewrites the cache, and the structured diff is surfaced — nothing is
    blocked here (PT-8/PT-13 decide pause).
  * Resilience is *reactive only* (decision ⑥): bounded-backoff reconnect on an observed drop, plus
    ``is_connected()`` health. The active heartbeat (PT-13) and pause (PT-8) live elsewhere.

No ib_async import in this file — that quarantine is the whole point of the port.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from ..config import Mode
from ..domain.models import InstrumentId, ValuationStatus
from ..telemetry import TelemetrySink
from .session import Session, signed_inventory_diff

# --------------------------------------------------------------------------- #
# telemetry stage names (§8 envelope) — named constants so they stay greppable
# --------------------------------------------------------------------------- #
STAGE_CONNECT = "ibkr.connect"
STAGE_DISCONNECT = "ibkr.disconnect"
STAGE_SNAPSHOT = "ibkr.snapshot"
STAGE_RECONCILE = "positions.reconcile"
STAGE_CLOCK_SKEW = "clock.skew"

#: ib_async summary tag -> RiskContext field. These three are the account fields PT-3 owns; every one is
#: REQUIRED (a missing one fails the snapshot closed). ``prices``/``data_as_of`` are PT-4, not here.
SUMMARY_TAG_MAP: dict[str, str] = {
    "NetLiquidation": "net_liquidation",
    "BuyingPower": "buying_power",
    "MaintMarginReq": "maintenance_margin",  # current requirement, NOT FullMaintMarginReq (decision ⑧)
}

#: ib_async error codes that mean the connection dropped (reconnect); 1102 = restored (health recovers).
ERROR_CODE_CONN_LOST = 1100
ERROR_CODE_CONN_RESTORED = 1102
ERROR_CODE_SOCKET_RESET = 1300
DISCONNECT_ERROR_CODES = frozenset({ERROR_CODE_CONN_LOST, ERROR_CODE_SOCKET_RESET})


# --------------------------------------------------------------------------- #
# exception hierarchy (decision ⑦) — never default; transient vs fatal is structural
# --------------------------------------------------------------------------- #
class IbkrGatewayError(Exception):
    """Root of PT-3's gateway errors. Callers catch this to distinguish gateway failures from bugs."""


class TransientGatewayError(IbkrGatewayError):
    """Retryable: the caller may retry while bounded-backoff reconnection runs. Not a misconfiguration."""


class FatalGatewayError(IbkrGatewayError):
    """Non-retryable: a misconfiguration surfaced at ``connect()``. Stops the run; no retry loop."""


class NotConnected(TransientGatewayError):
    """A read was attempted before ``connect()`` succeeded, or after the socket dropped."""


class SnapshotTimeout(TransientGatewayError):
    """The broker did not return warm account/position state within the timeout."""


class SnapshotIncomplete(TransientGatewayError):
    """A required account field was missing or unparseable — a partial snapshot is never returned."""


class AccountResolutionError(FatalGatewayError):
    """The account could not be resolved: unknown, absent, or ambiguous (multiple, none configured)."""


class PaperAssertionError(FatalGatewayError):
    """Under ``Mode.PAPER`` the resolved account was not a ``DU``-prefixed paper account."""


# --------------------------------------------------------------------------- #
# clock (decision ⑨) — injected UTC, deterministic in tests
# --------------------------------------------------------------------------- #
@runtime_checkable
class Clock(Protocol):
    """Source of the decision-clock ``as_of``. ``now()`` returns tz-aware UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock: ``datetime.now(UTC)``, tz-aware."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """A frozen clock for deterministic tests (and for pinning ``as_of`` in replays)."""

    moment: datetime

    def now(self) -> datetime:
        return self.moment


# --------------------------------------------------------------------------- #
# collaborator seams — typed Protocols so the money-critical path type-checks
# --------------------------------------------------------------------------- #
@runtime_checkable
class _PositionStore(Protocol):
    """The narrow PT-2 positions-cache seam the gateway reconciles against (conId-keyed; ADR-0002 ⑬)."""

    def all_positions(self) -> Mapping[InstrumentId, int]: ...
    def symbol_for_instrument_id(self, instrument_id: InstrumentId) -> str | None: ...
    def instrument_id_for_symbol(self, symbol: str) -> InstrumentId | None: ...
    def upsert_position(self, instrument_id: InstrumentId, symbol: str, quantity: int) -> None: ...


# --------------------------------------------------------------------------- #
# structured reconciliation result (decision ⑤) — report, don't gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PositionReconciliation:
    """The broker-vs-PT-2-cache position diff. Broker is truth; this is *reported*, never a gate.

    ``diffs`` maps an ``InstrumentId`` (conId) to ``(cache_qty, broker_qty)`` for every instrument whose
    held quantity differs. Both sides are normalized to non-zero holdings, so a stale cached ``0`` vs an
    absent broker instrument is not a spurious divergence. Held inventory now carries the broker ``conId``
    (ADR-0002 ⑫/⑬), so reconciliation keys on identity — never a symbol, never a manufactured id.
    """

    broker: dict[InstrumentId, int]
    cache: dict[InstrumentId, int]
    diffs: dict[InstrumentId, tuple[int, int]]

    @property
    def diverged(self) -> bool:
        return bool(self.diffs)


# --------------------------------------------------------------------------- #
# account snapshot (ADR-0002 ③/⑫) — the gateway's output; it NEVER builds RiskContext
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeldPosition:
    """One broker-authoritative holding, valuation-classified (ADR-0002 ⑫).

    ``valuation_status`` is a typed state: the snapshot is *complete about inventory* while explicitly
    reporting valuation degradation. Held value uses ``broker_market_value`` (ib_async ``marketValue`` --
    preserves broker multipliers), never ``quantity * price``. ``mark_available_at`` is the receipt time
    of the ``updatePortfolio`` event, not the account read time.
    """

    instrument_id: InstrumentId
    symbol: str
    quantity: int
    broker_mark: Decimal | None
    broker_market_value: Decimal | None
    mark_available_at: datetime | None
    valuation_status: ValuationStatus

    @classmethod
    def from_broker(
        cls,
        *,
        instrument_id: int,
        symbol: str,
        quantity: int,
        market_value: Decimal | None,
        market_price: Decimal | None,
        mark_available_at: datetime | None,
    ) -> HeldPosition:
        """Classify a raw broker holding fail-closed.

        AVAILABLE **only** when a market value *and* a mark receipt time are both present; otherwise
        UNAVAILABLE, and value/mark/price are forced to ``None`` (never a fabricated zero or a
        mark without provenance). This is the single shared classifier both adapters + the fake use.
        """
        available = market_value is not None and mark_available_at is not None
        if available:
            return cls(
                instrument_id=int(instrument_id),
                symbol=symbol,
                quantity=int(quantity),
                broker_mark=market_price,
                broker_market_value=market_value,
                mark_available_at=mark_available_at,
                valuation_status=ValuationStatus.AVAILABLE,
            )
        return cls(
            instrument_id=int(instrument_id),
            symbol=symbol,
            quantity=int(quantity),
            broker_mark=None,
            broker_market_value=None,
            mark_available_at=None,
            valuation_status=ValuationStatus.UNAVAILABLE,
        )


@dataclass(frozen=True)
class AccountSnapshot:
    """The gateway output (ADR-0002 ③): account money fields + ``observed_at`` + held inventory.

    Internal to the gateway→assembler seam — it never crosses the Risk & Sizing seam (only the assembler's
    sealed ``RiskContext`` does). ``generation`` is the ``IbkrSession`` generation this snapshot was read
    under; the assembler's fence rejects a cycle that spans generations.
    """

    net_liquidation: Decimal
    buying_power: Decimal
    maintenance_margin: Decimal
    held: tuple[HeldPosition, ...]
    observed_at: datetime
    generation: int


# --------------------------------------------------------------------------- #
# pure logic — shared by the real adapter and the fake (this is what CI actually tests)
# --------------------------------------------------------------------------- #
def build_account_snapshot(
    summary: Mapping[str, object],
    held: Iterable[HeldPosition],
    observed_at: datetime,
    generation: int,
) -> AccountSnapshot:
    """Map a broker account summary + held inventory into a frozen ``AccountSnapshot`` — or fail closed.

    Money fields parse straight from the summary *strings* to ``Decimal`` (never through ``float``, which
    would inject binary-rounding error into a money figure). Every required tag must be present and
    parseable; a missing or garbled one raises ``SnapshotIncomplete`` so a partial/zero-filled snapshot is
    never handed downstream (decision ⑦/⑧). The gateway never constructs ``RiskContext`` (ADR-0002 ③).
    """
    fields: dict[str, Decimal] = {}
    missing: list[str] = []
    for tag, field_name in SUMMARY_TAG_MAP.items():
        raw = summary.get(tag)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            missing.append(tag)
            continue
        try:
            # str() is a no-op on the broker's string values; it only guards a stray non-string from
            # taking the float path. The parse itself is string -> Decimal, exact.
            fields[field_name] = Decimal(str(raw))
        except InvalidOperation, ValueError:
            missing.append(tag)
    if missing:
        raise SnapshotIncomplete(f"account summary missing/unparseable required tags: {sorted(missing)}")
    return AccountSnapshot(held=tuple(held), observed_at=observed_at, generation=generation, **fields)


def resolve_account(configured: str | None, available: Iterable[str], mode: Mode) -> str:
    """Resolve the account fail-closed, then assert the paper guard under ``PAPER`` (decision ④).

    * configured & present in ``available`` -> use it.
    * configured & absent -> ``AccountResolutionError``.
    * unset & exactly one account -> use it.
    * unset & zero or multiple -> ``AccountResolutionError`` (never silently pick one).
    * ``Mode.PAPER`` -> the resolved account must be ``DU``-prefixed else ``PaperAssertionError``.
    """
    accounts = [a for a in available if a]
    if configured:
        if configured not in accounts:
            raise AccountResolutionError(
                f"configured account {configured!r} is not in broker accounts {accounts}"
            )
        resolved = configured
    elif len(accounts) == 1:
        resolved = accounts[0]
    elif not accounts:
        raise AccountResolutionError("broker returned no accounts to resolve")
    else:
        raise AccountResolutionError(
            f"account unset and multiple accounts present {accounts}; set IBKR_ACCOUNT to disambiguate"
        )
    if mode is Mode.PAPER and not resolved.startswith("DU"):
        raise PaperAssertionError(f"account {resolved!r} is not a DU-prefixed paper account under Mode.PAPER")
    return resolved


def reconcile_positions(
    broker: Mapping[InstrumentId, int], cache: Mapping[InstrumentId, int]
) -> PositionReconciliation:
    """Diff broker truth against the PT-2 cache. Non-zero holdings only; conId -> (cache, broker)."""
    # Shared signed-inventory diff: left=cache, right=broker, so diffs are keyed (cache_qty, broker_qty)
    # to match PositionReconciliation's contract.
    diff = signed_inventory_diff(cache, broker)
    return PositionReconciliation(broker=diff.right, cache=diff.left, diffs=diff.diffs)


def _as_utc(dt: datetime) -> datetime:
    """A tz-aware UTC datetime: a naive value is assumed UTC (the system convention, matching the live
    adapter's broker-time normalization); an aware value is converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def clock_skew_seconds(local: datetime, broker: datetime | None) -> float | None:
    """Absolute skew between the injected clock and broker time, or ``None`` if broker time is absent.

    Defensive about tz-awareness: a naive datetime on either side is normalized to UTC before subtracting
    (ADR ⑨ 'tz-aware UTC throughout'), so a naive broker time (the fake's skew knob) or a mis-injected naive
    ``Clock`` can never raise ``TypeError`` on the snapshot path — skew is *reported*, never gates (⑤/⑦)."""
    if broker is None:
        return None
    return abs((_as_utc(broker) - _as_utc(local)).total_seconds())


def backoff_delays(attempts: int, base: float, cap: float) -> list[float]:
    """Bounded exponential backoff schedule (deterministic; no jitter, so tests are reproducible)."""
    return [min(cap, base * (2**i)) for i in range(attempts)]


# --------------------------------------------------------------------------- #
# the port (decision ①) — downstream depends on THIS, never on ib_async
# --------------------------------------------------------------------------- #
@runtime_checkable
class AccountGateway(Protocol):
    """The domain port: a broker connection that yields an ``AccountSnapshot`` (ADR-0002 ③).

    The ``DecisionContextAssembler`` holds one of these and calls ``snapshot()``; it never imports
    ib_async. The gateway no longer constructs ``RiskContext`` — only the assembler mints one. The real
    (``IbkrAccountGateway``) and fake (``FakeAccountGateway``) implementations are interchangeable.
    """

    async def connect(self) -> None: ...
    async def snapshot(self) -> AccountSnapshot: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    @property
    def generation(self) -> int: ...


# --------------------------------------------------------------------------- #
# template-method base (decisions ②/⑤/⑥/⑨) — ib_async-free orchestration
# --------------------------------------------------------------------------- #
class _BaseAccountGateway:
    """Shared orchestration for both gateways. Subclasses fill the ``_raw_*``/``_fetch_*`` hooks; the
    connect → resolve → snapshot → reconcile → telemetry shape (and reactive reconnect) lives here so the
    fake and the live adapter run the same load-bearing code.
    """

    def __init__(
        self,
        *,
        config: object,
        mode: Mode,
        clock: Clock | None = None,
        store: _PositionStore | None = None,
        emitter: TelemetrySink | None = None,
        session: Session | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        reconnect_attempts: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        skew_threshold: float = 2.0,
    ) -> None:
        self._config = config
        self._mode = mode
        self._clock: Clock = clock or SystemClock()
        self._store = store
        self._emitter = emitter
        # ADR-0002 ⑨: the IbkrSession is the sole lifecycle owner of the generation. When one is injected
        # (the real adapter, or a fake session shared with the market feed) generation is read from it so a
        # reconnect fence spans both the account read and the quote batch. Absent a session (simple fake
        # tests) the base owns a local counter bumped on reconnect.
        self._session = session
        self._generation = 0
        # Injected so tests drive backoff with zero wall-clock; defaults to real asyncio.sleep.
        self._sleep = sleep or asyncio.sleep
        self._reconnect_attempts = reconnect_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._skew_threshold = skew_threshold
        self._connected = False
        self._account: str | None = None
        self._last_reconciliation: PositionReconciliation | None = None

    # ---- hooks the real adapter / fake implement -------------------------- #
    async def _raw_connect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _raw_disconnect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _fetch_accounts(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _fetch_summary(self) -> Mapping[str, object]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _fetch_held_positions(self) -> Iterable[HeldPosition]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _fetch_broker_time(self) -> datetime | None:  # pragma: no cover - overridden
        return None

    # ---- public port surface --------------------------------------------- #
    @property
    def account(self) -> str | None:
        """The resolved account id (set at ``connect()``). Never emitted in telemetry (PII)."""
        return self._account

    @property
    def last_reconciliation(self) -> PositionReconciliation | None:
        """The structured result of the most recent ``snapshot()`` reconciliation (decision ⑤)."""
        return self._last_reconciliation

    @property
    def generation(self) -> int:
        """The session generation (ADR-0002 ⑨). Bumped on every reconnect; the assembler's fence rejects
        a cycle whose account snapshot and quotes disagree on it (a spliced pre-drop/post-reconnect read)."""
        session = self._session
        if session is not None:
            return int(session.generation)
        return self._generation

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Open the (read-only) session with bounded-backoff, then resolve the account fail-closed.

        Account resolution runs *once*, after the socket is up, and is **not** retried — a fatal
        misconfiguration (ambiguous account / non-paper account) stops the run rather than spinning a
        reconnect loop against something that will never succeed (decision ④/⑦).
        """
        await self._connect_with_backoff()
        self._account = resolve_account(self._configured_account(), await self._fetch_accounts(), self._mode)
        self._connected = True
        self._emit(STAGE_CONNECT, metrics={"readonly": self._is_readonly(), "reconnect": False})

    async def snapshot(self) -> AccountSnapshot:
        """Read warm account/held state into a frozen ``AccountSnapshot``; reconcile + emit as a side task.

        The gateway **never** constructs ``RiskContext`` (ADR-0002 ③) — only the ``DecisionContextAssembler``
        mints one. Fail-closed: not connected -> ``NotConnected``; a missing field -> ``SnapshotIncomplete``
        (raised inside ``build_account_snapshot``). Reconciliation and clock-skew are *reported*, never gate.
        """
        if not self._connected:
            raise NotConnected("snapshot() called before connect() (or after the connection dropped)")
        generation = self.generation
        summary = await self._fetch_summary()
        held = tuple(await self._fetch_held_positions())
        observed_at = self._clock.now()
        account = build_account_snapshot(summary, held, observed_at, generation)  # fail-closed
        self._reconcile(held)
        self._check_skew(await self._fetch_broker_time(), observed_at)
        unavailable = sum(1 for h in held if h.valuation_status is ValuationStatus.UNAVAILABLE)
        self._emit(
            STAGE_SNAPSHOT,
            metrics={
                "net_liquidation": str(account.net_liquidation),
                "buying_power": str(account.buying_power),
                "maintenance_margin": str(account.maintenance_margin),
                "position_count": len(account.held),
                "unavailable_valuations": unavailable,
                "generation": generation,
            },
        )
        return account

    async def disconnect(self) -> None:
        self._connected = False
        await self._raw_disconnect()
        self._emit(STAGE_DISCONNECT, metrics={"reason": "explicit"})

    # ---- reactive resilience (decision ⑥) -------------------------------- #
    async def reconnect(self) -> None:
        """Re-establish a dropped session (bounded backoff) and re-resolve the account; health recovers.

        Reconnect **bumps the generation** (ADR-0002 ⑨) so a cycle that spans the drop is fenced off. When
        an ``IbkrSession`` is injected it owns the generation and bumps it during its own reconnect; absent
        one, the base owns the counter and bumps it here.
        """
        self._connected = False
        await self._connect_with_backoff()
        self._account = resolve_account(self._configured_account(), await self._fetch_accounts(), self._mode)
        session = self._session
        if session is not None:
            session.bump_generation()
        else:
            self._generation += 1
        self._connected = True
        self._emit(
            STAGE_CONNECT,
            metrics={
                "readonly": self._is_readonly(),
                "reconnect": True,
                "generation": self.generation,
            },
        )

    async def _on_disconnect(self) -> None:
        """Reactive drop handler (wired to ``disconnectedEvent`` / error 1100/1300 in the real adapter).

        Marks the gateway unhealthy, emits ``ibkr.disconnect``, and drives a bounded-backoff reconnect.
        Idempotent: a duplicate event while already disconnected is a no-op.
        """
        if not self._connected:
            return
        self._connected = False
        self._emit(STAGE_DISCONNECT, metrics={"reason": "connection-drop"})
        await self.reconnect()

    async def _connect_with_backoff(self) -> None:
        """Retry ``_raw_connect`` on transient failures with a bounded exponential schedule. A fatal
        error (misconfiguration) is never retried — it propagates immediately."""
        delays = backoff_delays(self._reconnect_attempts, self._backoff_base, self._backoff_max)
        last_exc: BaseException | None = None
        for i, delay in enumerate(delays):
            try:
                await self._raw_connect()
                return
            except FatalGatewayError:
                raise
            except Exception as exc:  # transient socket failure — back off and retry
                last_exc = exc
                if i < len(delays) - 1:
                    await self._sleep(delay)
        raise NotConnected(f"connect failed after {self._reconnect_attempts} attempts") from last_exc

    # ---- reconciliation + skew (side effects, never gates) --------------- #
    def _reconcile(self, held: Iterable[HeldPosition]) -> PositionReconciliation:
        # Held inventory now carries the broker conId (ADR-0002 ⑫/⑬), and the PT-4a store is conId-keyed,
        # so reconciliation is identity-vs-identity — no symbol translation, no manufactured id. Broker is
        # truth: divergence rewrites the cache by conId (carrying the display symbol) and emits, never gates.
        held = tuple(held)
        broker: dict[InstrumentId, int] = {h.instrument_id: h.quantity for h in held}
        symbols: dict[InstrumentId, str] = {h.instrument_id: h.symbol for h in held}
        cache: dict[InstrumentId, int] = {}
        if self._store is not None:
            cache = dict(self._store.all_positions())
            for instrument_id in cache:
                symbols.setdefault(instrument_id, self._store.symbol_for_instrument_id(instrument_id) or "")
        recon = reconcile_positions(broker, cache)
        self._last_reconciliation = recon
        if recon.diverged:
            if self._store is not None:
                for instrument_id, quantity in recon.broker.items():
                    self._store.upsert_position(instrument_id, symbols.get(instrument_id, ""), quantity)
                for instrument_id in recon.cache:
                    if instrument_id not in recon.broker:
                        self._store.upsert_position(instrument_id, symbols.get(instrument_id, ""), 0)
            self._emit(
                STAGE_RECONCILE,
                metrics={
                    "diverged_instruments": len(recon.diffs),
                    "diffs": {str(k): {"cache": c, "broker": b} for k, (c, b) in recon.diffs.items()},
                },
            )
        return recon

    def _check_skew(self, broker_time: datetime | None, local_time: datetime) -> None:
        skew = clock_skew_seconds(local_time, broker_time)
        if skew is not None and skew > self._skew_threshold:
            self._emit(
                STAGE_CLOCK_SKEW,
                metrics={"skew_seconds": skew, "threshold_seconds": self._skew_threshold},
            )

    # ---- helpers ---------------------------------------------------------- #
    def _configured_account(self) -> str | None:
        return getattr(self._config, "account", None)

    def _is_readonly(self) -> bool:
        return bool(getattr(self._config, "readonly", True))

    def _emit(self, stage: str, *, metrics: dict | None = None) -> None:
        """Best-effort §8 event. Telemetry must never break the gateway (same rule as the core emitter)."""
        if self._emitter is None:
            return
        # Observability is best-effort: a telemetry failure must never take down the read path.
        with contextlib.suppress(Exception):
            self._emitter.emit(stage=stage, agent_role="ibkr-gateway", task_id="PT-3", metrics=metrics or {})
