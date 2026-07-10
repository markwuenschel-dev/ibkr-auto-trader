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
from ..domain import RiskContext

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
# structured reconciliation result (decision ⑤) — report, don't gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PositionReconciliation:
    """The broker-vs-PT-2-cache position diff. Broker is truth; this is *reported*, never a gate.

    ``diffs`` maps a symbol to ``(cache_qty, broker_qty)`` for every symbol whose held quantity differs.
    Both sides are normalized to non-zero holdings, so a stale cached ``0`` vs an absent broker symbol is
    not a spurious divergence.
    """

    broker: dict[str, int]
    cache: dict[str, int]
    diffs: dict[str, tuple[int, int]]

    @property
    def diverged(self) -> bool:
        return bool(self.diffs)


# --------------------------------------------------------------------------- #
# pure logic — shared by the real adapter and the fake (this is what CI actually tests)
# --------------------------------------------------------------------------- #
def build_risk_context(
    summary: Mapping[str, object],
    positions: Mapping[str, object],
    as_of: datetime,
) -> RiskContext:
    """Map a broker account summary + positions into a frozen ``RiskContext`` — or fail closed.

    Money fields parse straight from the summary *strings* to ``Decimal`` (never through ``float``, which
    would inject binary-rounding error into a money figure). Every required tag must be present and
    parseable; a missing or garbled one raises ``SnapshotIncomplete`` so a partial/zero-filled context is
    never handed downstream (decision ⑦/⑧).
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
        except (InvalidOperation, ValueError):
            missing.append(tag)
    if missing:
        raise SnapshotIncomplete(
            f"account summary missing/unparseable required tags: {sorted(missing)}"
        )
    signed_positions = {sym: int(qty) for sym, qty in positions.items()}
    return RiskContext(as_of=as_of, positions=signed_positions, **fields)


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
        raise PaperAssertionError(
            f"account {resolved!r} is not a DU-prefixed paper account under Mode.PAPER"
        )
    return resolved


def reconcile_positions(
    broker: Mapping[str, object], cache: Mapping[str, object]
) -> PositionReconciliation:
    """Diff broker truth against the PT-2 cache. Non-zero holdings only; symbol -> (cache, broker)."""
    broker_nz = {s: int(q) for s, q in broker.items() if int(q) != 0}
    cache_nz = {s: int(q) for s, q in cache.items() if int(q) != 0}
    diffs: dict[str, tuple[int, int]] = {}
    for sym in set(broker_nz) | set(cache_nz):
        b = broker_nz.get(sym, 0)
        c = cache_nz.get(sym, 0)
        if b != c:
            diffs[sym] = (c, b)
    return PositionReconciliation(broker=broker_nz, cache=cache_nz, diffs=diffs)


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
    """The domain port: a broker connection that yields a frozen ``RiskContext``.

    The risk pipeline holds one of these and calls ``snapshot()``; it never imports ib_async. The real
    (``IbkrAccountGateway``) and fake (``FakeAccountGateway``) implementations are interchangeable.
    """

    async def connect(self) -> None: ...
    async def snapshot(self) -> RiskContext: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...


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
        store: object | None = None,
        emitter: object | None = None,
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

    async def _fetch_positions(self) -> Mapping[str, object]:  # pragma: no cover - overridden
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

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Open the (read-only) session with bounded-backoff, then resolve the account fail-closed.

        Account resolution runs *once*, after the socket is up, and is **not** retried — a fatal
        misconfiguration (ambiguous account / non-paper account) stops the run rather than spinning a
        reconnect loop against something that will never succeed (decision ④/⑦).
        """
        await self._connect_with_backoff()
        self._account = resolve_account(
            self._configured_account(), await self._fetch_accounts(), self._mode
        )
        self._connected = True
        self._emit(STAGE_CONNECT, metrics={"readonly": self._is_readonly(), "reconnect": False})

    async def snapshot(self) -> RiskContext:
        """Read warm account/position state into a frozen ``RiskContext``; reconcile + emit as a side task.

        Fail-closed: not connected -> ``NotConnected``; a missing field -> ``SnapshotIncomplete`` (raised
        inside ``build_risk_context``). Reconciliation and clock-skew are *reported*, never gate the read.
        """
        if not self._connected:
            raise NotConnected("snapshot() called before connect() (or after the connection dropped)")
        summary = await self._fetch_summary()
        positions = await self._fetch_positions()
        as_of = self._clock.now()
        ctx = build_risk_context(summary, positions, as_of)  # raises SnapshotIncomplete, fail-closed
        self._reconcile(positions)
        self._check_skew(await self._fetch_broker_time(), as_of)
        self._emit(
            STAGE_SNAPSHOT,
            metrics={
                "net_liquidation": str(ctx.net_liquidation),
                "buying_power": str(ctx.buying_power),
                "maintenance_margin": str(ctx.maintenance_margin),
                "position_count": len(ctx.positions),
            },
        )
        return ctx

    async def disconnect(self) -> None:
        self._connected = False
        await self._raw_disconnect()
        self._emit(STAGE_DISCONNECT, metrics={"reason": "explicit"})

    # ---- reactive resilience (decision ⑥) -------------------------------- #
    async def reconnect(self) -> None:
        """Re-establish a dropped session (bounded backoff) and re-resolve the account; health recovers."""
        self._connected = False
        await self._connect_with_backoff()
        self._account = resolve_account(
            self._configured_account(), await self._fetch_accounts(), self._mode
        )
        self._connected = True
        self._emit(STAGE_CONNECT, metrics={"readonly": self._is_readonly(), "reconnect": True})

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
        raise NotConnected(
            f"connect failed after {self._reconnect_attempts} attempts"
        ) from last_exc

    # ---- reconciliation + skew (side effects, never gates) --------------- #
    def _reconcile(self, broker_positions: Mapping[str, object]) -> PositionReconciliation:
        cache = self._store.all_positions() if self._store is not None else {}
        recon = reconcile_positions(broker_positions, cache)
        self._last_reconciliation = recon
        if recon.diverged:
            if self._store is not None:
                # Broker is truth: rewrite the cache. Symbols the broker no longer holds go flat (0),
                # since the PT-2 store has no delete and ``position()`` treats absent == 0.
                for sym, qty in recon.broker.items():
                    self._store.upsert_position(sym, qty)
                for sym in recon.cache:
                    if sym not in recon.broker:
                        self._store.upsert_position(sym, 0)
            self._emit(
                STAGE_RECONCILE,
                metrics={
                    "diverged_symbols": len(recon.diffs),
                    "diffs": {s: {"cache": c, "broker": b} for s, (c, b) in recon.diffs.items()},
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
            self._emitter.emit(
                stage=stage, agent_role="ibkr-gateway", task_id="PT-3", metrics=metrics or {}
            )
