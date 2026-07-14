"""PT-3/PT-4b IBKR gateway tests — the ``AccountSnapshot`` DoD surface, all via ``FakeAccountGateway``.

No live socket, no ib_async, no wall-clock. Coroutines are driven with ``asyncio.run`` (the dev env has no
pytest-asyncio), and the fake's backoff ``sleep`` is a no-op, so the whole file is fast and deterministic.
Because the fake and the real adapter share ``_BaseAccountGateway`` + the pure helpers, these tests cover
the actual mapping / resolution / reconciliation / reconnect logic the live gateway runs — only the socket
plumbing (``_fetch_*``) is fake. The gateway no longer builds ``RiskContext`` (ADR-0002 ③); it yields an
``AccountSnapshot`` and the ``DecisionContextAssembler`` mints the context (see ``test_assembler.py``).

The one live-Gateway test at the bottom is marked ``integration`` and skipped unless ``IBKR_INTEGRATION``
is set — excluded from CI, run manually.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ibkr_trader.config import Mode
from ibkr_trader.domain import ValuationStatus
from ibkr_trader.ibkr import (
    AccountGateway,
    AccountResolutionError,
    AccountSnapshot,
    FakeAccountGateway,
    FatalGatewayError,
    FixedClock,
    HeldPosition,
    IbkrConnectionConfig,
    IbkrGatewayError,
    NotConnected,
    PaperAssertionError,
    SnapshotIncomplete,
    TransientGatewayError,
    build_account_snapshot,
    reconcile_positions,
    resolve_account,
)
from ibkr_trader.ibkr.config import DEFAULT_HOST, DEFAULT_PAPER_PORT
from ibkr_trader.ibkr.gateway import clock_skew_seconds
from ibkr_trader.state import StateStore
from ibkr_trader.telemetry import Emitter

_MOMENT = datetime(2026, 7, 9, 14, 30, tzinfo=UTC)

# Stable broker conIds for the display symbols used across these tests.
AAPL, MSFT, TSLA = 265598, 272093, 76792991


@dataclass
class RecordingEmitter(Emitter):
    """A real §8 ``Emitter`` that captures events in memory instead of writing them to disk — keeps the
    envelope + content-hash but lets tests assert on emitted stages/metrics."""

    events: list = field(default_factory=list)

    def _append(self, event: dict) -> None:  # override: capture, do not touch the filesystem
        self.events.append(event)


def _run(coro):
    return asyncio.run(coro)


def _stages(emitter: RecordingEmitter, stage: str) -> list[dict]:
    return [e for e in emitter.events if e["stage"] == stage]


def _paper_summary() -> dict[str, str]:
    return {
        "NetLiquidation": "2000.50",
        "BuyingPower": "8000.00",
        "MaintMarginReq": "150.25",
    }


def _held(
    con_id: int,
    symbol: str,
    quantity: int,
    *,
    market_value: str | None = None,
    market_price: str | None = None,
    mark_at: datetime | None = None,
) -> HeldPosition:
    """Build a HeldPosition via the shared broker classifier. AVAILABLE iff value + mark are both given."""
    return HeldPosition.from_broker(
        instrument_id=con_id,
        symbol=symbol,
        quantity=quantity,
        market_value=Decimal(market_value) if market_value is not None else None,
        market_price=Decimal(market_price) if market_price is not None else None,
        mark_available_at=mark_at,
    )


def _make_gateway(**kwargs) -> FakeAccountGateway:
    kwargs.setdefault("clock", FixedClock(_MOMENT))
    return FakeAccountGateway(**kwargs)


# --------------------------------------------------------------------------- #
# pure helpers — precise, fast coverage of the load-bearing logic
# --------------------------------------------------------------------------- #
class TestPureMapping:
    def test_summary_tags_map_to_account_fields(self):
        account = build_account_snapshot(
            _paper_summary(), [_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4)], _MOMENT, 0
        )
        assert account.net_liquidation == Decimal("2000.50")
        assert account.buying_power == Decimal("8000.00")
        assert account.maintenance_margin == Decimal("150.25")
        assert account.observed_at == _MOMENT
        assert account.generation == 0

    def test_money_is_decimal_from_string_not_float(self):
        # A float round-trip of "100.10" is 100.09999999999999; Decimal-from-string stays exact.
        account = build_account_snapshot(
            {"NetLiquidation": "100.10", "BuyingPower": "0.30", "MaintMarginReq": "0"}, [], _MOMENT, 0
        )
        assert account.net_liquidation == Decimal("100.10")
        assert str(account.net_liquidation) == "100.10"
        assert account.buying_power == Decimal("0.30")

    def test_held_quantities_are_signed_ints(self):
        account = build_account_snapshot(
            _paper_summary(),
            [_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4), _held(TSLA, "TSLA", 0)],
            _MOMENT,
            0,
        )
        by_id = {h.instrument_id: h.quantity for h in account.held}
        assert by_id == {AAPL: 10, MSFT: -4, TSLA: 0}
        assert all(isinstance(h.quantity, int) for h in account.held)

    def test_valuation_status_is_fail_closed(self):
        # value + mark present -> AVAILABLE; either missing -> UNAVAILABLE with neither carried.
        available = _held(AAPL, "AAPL", 10, market_value="1500", market_price="150", mark_at=_MOMENT)
        assert available.valuation_status is ValuationStatus.AVAILABLE
        assert available.broker_market_value == Decimal("1500")

        no_mark = _held(MSFT, "MSFT", 4, market_value="1000", market_price="250")  # value but no mark time
        assert no_mark.valuation_status is ValuationStatus.UNAVAILABLE
        assert no_mark.broker_market_value is None
        assert no_mark.mark_available_at is None

    def test_missing_required_field_raises_snapshot_incomplete(self):
        with pytest.raises(SnapshotIncomplete):
            build_account_snapshot({"NetLiquidation": "1", "BuyingPower": "1"}, [], _MOMENT, 0)  # no margin

    def test_blank_field_is_treated_as_missing(self):
        with pytest.raises(SnapshotIncomplete):
            build_account_snapshot(
                {"NetLiquidation": "1", "BuyingPower": "", "MaintMarginReq": "1"}, [], _MOMENT, 0
            )

    def test_unparseable_field_raises_snapshot_incomplete(self):
        with pytest.raises(SnapshotIncomplete):
            build_account_snapshot(
                {"NetLiquidation": "not-a-number", "BuyingPower": "1", "MaintMarginReq": "1"},
                [],
                _MOMENT,
                0,
            )


class TestPureResolution:
    def test_configured_account_present(self):
        assert resolve_account("DU111", ["DU111", "DU222"], Mode.PAPER) == "DU111"

    def test_configured_account_absent_raises(self):
        with pytest.raises(AccountResolutionError):
            resolve_account("DU999", ["DU111"], Mode.PAPER)

    def test_sole_account_used_when_unset(self):
        assert resolve_account(None, ["DU111"], Mode.PAPER) == "DU111"

    def test_ambiguous_account_raises(self):
        with pytest.raises(AccountResolutionError):
            resolve_account(None, ["DU111", "DU222"], Mode.PAPER)

    def test_no_accounts_raises(self):
        with pytest.raises(AccountResolutionError):
            resolve_account(None, [], Mode.PAPER)

    def test_non_du_under_paper_raises(self):
        with pytest.raises(PaperAssertionError):
            resolve_account(None, ["U1234567"], Mode.PAPER)

    def test_non_du_allowed_under_live(self):
        assert resolve_account(None, ["U1234567"], Mode.LIVE) == "U1234567"


class TestPureReconcile:
    def test_divergence_detected_signed(self):
        recon = reconcile_positions({AAPL: 10, MSFT: -4}, {AAPL: 5, TSLA: 3})
        assert recon.diverged
        assert recon.diffs == {AAPL: (5, 10), MSFT: (0, -4), TSLA: (3, 0)}

    def test_no_divergence_when_equal(self):
        recon = reconcile_positions({AAPL: 10}, {AAPL: 10})
        assert not recon.diverged
        assert recon.diffs == {}

    def test_zero_holdings_are_not_spurious_divergence(self):
        # A stale cached 0 vs an absent broker instrument is the same "flat" — not a divergence.
        recon = reconcile_positions({AAPL: 10}, {AAPL: 10, TSLA: 0})
        assert not recon.diverged


# --------------------------------------------------------------------------- #
# end-to-end via the fake gateway (same orchestration the live adapter runs)
# --------------------------------------------------------------------------- #
class TestSnapshotMapping:
    def test_snapshot_returns_account_snapshot(self):
        gw = _make_gateway(summary=_paper_summary(), held=[_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4)])
        _run(gw.connect())
        account = _run(gw.snapshot())
        assert isinstance(account, AccountSnapshot)
        assert account.net_liquidation == Decimal("2000.50")
        assert account.buying_power == Decimal("8000.00")
        assert account.maintenance_margin == Decimal("150.25")
        assert {h.instrument_id: h.quantity for h in account.held} == {AAPL: 10, MSFT: -4}
        assert account.observed_at == _MOMENT
        assert account.generation == 0
        # The gateway never mints a RiskContext (ADR-0002 ③) — that is the assembler's job.
        assert not hasattr(account, "prices")

    def test_available_and_unavailable_holdings_are_typed(self):
        gw = _make_gateway(
            summary=_paper_summary(),
            held=[
                _held(AAPL, "AAPL", 10, market_value="1500", market_price="150", mark_at=_MOMENT),
                _held(MSFT, "MSFT", -4),  # no mark -> UNAVAILABLE
            ],
        )
        _run(gw.connect())
        account = _run(gw.snapshot())
        by_id = {h.instrument_id: h for h in account.held}
        assert by_id[AAPL].valuation_status is ValuationStatus.AVAILABLE
        assert by_id[AAPL].broker_market_value == Decimal("1500")
        assert by_id[MSFT].valuation_status is ValuationStatus.UNAVAILABLE
        assert by_id[MSFT].broker_market_value is None


class TestReconciliation:
    def test_divergence_emits_event_rewrites_cache_and_does_not_gate(self, tmp_path):
        emitter = RecordingEmitter()
        with StateStore(tmp_path / "trader.db") as store:
            store.upsert_position(AAPL, "AAPL", 5)  # cache disagrees with broker
            store.upsert_position(TSLA, "TSLA", 3)  # broker no longer holds this
            gw = _make_gateway(
                summary=_paper_summary(),
                held=[_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4)],
                store=store,
                emitter=emitter,
            )
            _run(gw.connect())
            account = _run(gw.snapshot())  # must NOT raise — reconciliation reports, never gates

            assert isinstance(account, AccountSnapshot)
            assert _stages(emitter, "positions.reconcile")  # event emitted
            # broker truth written back by conId; TSLA (absent at broker) goes flat.
            assert store.all_positions() == {AAPL: 10, MSFT: -4, TSLA: 0}
            assert gw.last_reconciliation is not None and gw.last_reconciliation.diverged

    def test_matching_cache_emits_no_reconcile_event(self, tmp_path):
        emitter = RecordingEmitter()
        with StateStore(tmp_path / "trader.db") as store:
            store.upsert_position(AAPL, "AAPL", 10)
            gw = _make_gateway(
                summary=_paper_summary(),
                held=[_held(AAPL, "AAPL", 10)],
                store=store,
                emitter=emitter,
            )
            _run(gw.connect())
            _run(gw.snapshot())
            assert _stages(emitter, "positions.reconcile") == []
            assert gw.last_reconciliation is not None and not gw.last_reconciliation.diverged


class TestFailClosed:
    def test_missing_field_raises_snapshot_incomplete_and_returns_nothing(self):
        gw = _make_gateway(
            summary={"NetLiquidation": "2000", "BuyingPower": "8000"},  # no MaintMarginReq
            held=[],
        )
        _run(gw.connect())
        with pytest.raises(SnapshotIncomplete):
            _run(gw.snapshot())  # a partial AccountSnapshot is never returned

    def test_unknown_account_raises_fatal_at_connect(self):
        gw = _make_gateway(config=IbkrConnectionConfig(account="DU999"), accounts=["DU1234567"])
        with pytest.raises(AccountResolutionError) as exc:
            _run(gw.connect())
        assert isinstance(exc.value, FatalGatewayError)
        assert not gw.is_connected()

    def test_ambiguous_account_raises_fatal_at_connect(self):
        gw = _make_gateway(accounts=["DU111", "DU222"])  # unset + multiple
        with pytest.raises(AccountResolutionError):
            _run(gw.connect())

    def test_non_du_under_paper_raises_paper_assertion(self):
        gw = _make_gateway(mode=Mode.PAPER, accounts=["U1234567"])
        with pytest.raises(PaperAssertionError) as exc:
            _run(gw.connect())
        assert isinstance(exc.value, FatalGatewayError)

    def test_fatal_resolution_is_not_retried(self):
        gw = _make_gateway(config=IbkrConnectionConfig(account="DU999"), accounts=["DU1234567"])
        with pytest.raises(AccountResolutionError):
            _run(gw.connect())
        assert gw.connect_attempts == 1  # the socket came up once; resolution failed and did NOT loop

    def test_snapshot_before_connect_raises_not_connected(self):
        gw = _make_gateway(summary=_paper_summary(), held=[])
        with pytest.raises(NotConnected):
            _run(gw.snapshot())

    def test_error_taxonomy(self):
        # transient vs fatal is structural, and both root at IbkrGatewayError.
        assert issubclass(SnapshotIncomplete, TransientGatewayError)
        assert issubclass(NotConnected, TransientGatewayError)
        assert issubclass(AccountResolutionError, FatalGatewayError)
        assert issubclass(PaperAssertionError, FatalGatewayError)
        assert issubclass(TransientGatewayError, IbkrGatewayError)
        assert issubclass(FatalGatewayError, IbkrGatewayError)


class TestResilience:
    def test_initial_connect_backs_off_then_succeeds(self):
        gw = _make_gateway(summary=_paper_summary(), held=[])
        gw.fail_next(2)  # two transient socket failures before success
        _run(gw.connect())
        assert gw.is_connected()
        assert gw.connect_attempts == 3  # 2 failed + 1 success

    def test_drop_then_reconnect_recovers_health_and_bumps_generation(self):
        gw = _make_gateway(summary=_paper_summary(), held=[])
        _run(gw.connect())
        assert gw.is_connected()
        assert gw.generation == 0

        gw.simulate_drop()
        assert not gw.is_connected()

        gw.fail_next(2)  # reconnect must survive a couple of transient failures
        _run(gw.simulate_reconnect())
        assert gw.is_connected()
        assert gw.generation == 1  # reconnect fences the cycle (ADR-0002 ⑨)
        # snapshot works again after recovery, carrying the new generation
        account = _run(gw.snapshot())
        assert isinstance(account, AccountSnapshot)
        assert account.generation == 1

    def test_disconnect_event_emits_and_recovers(self):
        emitter = RecordingEmitter()
        gw = _make_gateway(summary=_paper_summary(), held=[], emitter=emitter)
        _run(gw.connect())
        _run(gw.simulate_disconnect_event())  # drop -> ibkr.disconnect -> backoff reconnect
        assert gw.is_connected()
        assert _stages(emitter, "ibkr.disconnect")
        assert len(_stages(emitter, "ibkr.connect")) >= 2  # initial + reconnect

    def test_backoff_gives_up_and_raises_not_connected(self):
        gw = _make_gateway(summary=_paper_summary(), held=[], reconnect_attempts=3)
        gw.fail_next(10)  # never recovers within the bound
        with pytest.raises(NotConnected):
            _run(gw.connect())
        assert not gw.is_connected()
        assert gw.connect_attempts == 3


class TestReadOnly:
    def test_config_is_readonly_by_default(self):
        assert IbkrConnectionConfig().readonly is True

    def test_from_env_is_readonly_by_default(self):
        assert IbkrConnectionConfig.from_env().readonly is True

    def test_connect_telemetry_reports_readonly(self):
        emitter = RecordingEmitter()
        gw = _make_gateway(summary=_paper_summary(), held=[], emitter=emitter)
        _run(gw.connect())
        connect_events = _stages(emitter, "ibkr.connect")
        assert connect_events and connect_events[0]["metrics"]["readonly"] is True


class TestTelemetryEnvelope:
    def test_snapshot_event_has_money_and_count_no_pii(self):
        emitter = RecordingEmitter()
        gw = _make_gateway(
            config=IbkrConnectionConfig(account="DU1234567"),
            accounts=["DU1234567"],
            summary=_paper_summary(),
            held=[_held(AAPL, "AAPL", 10), _held(MSFT, "MSFT", -4)],
            emitter=emitter,
        )
        _run(gw.connect())
        _run(gw.snapshot())
        snap = _stages(emitter, "ibkr.snapshot")
        assert len(snap) == 1
        metrics = snap[0]["metrics"]
        assert metrics["net_liquidation"] == "2000.50"
        assert metrics["buying_power"] == "8000.00"
        assert metrics["maintenance_margin"] == "150.25"
        assert metrics["position_count"] == 2
        assert metrics["unavailable_valuations"] == 2  # neither holding carried a mark
        # no account id / PII anywhere in the snapshot event.
        assert "account" not in metrics
        assert "DU1234567" not in str(snap[0])


class TestClockSkew:
    def test_skew_beyond_threshold_emits_event(self):
        emitter = RecordingEmitter()
        gw = _make_gateway(
            summary=_paper_summary(),
            held=[],
            emitter=emitter,
            broker_time=_MOMENT + timedelta(seconds=5),  # 5s drift, threshold 2s
            skew_threshold=2.0,
        )
        _run(gw.connect())
        _run(gw.snapshot())
        assert _stages(emitter, "clock.skew")

    def test_skew_within_threshold_is_quiet(self):
        emitter = RecordingEmitter()
        gw = _make_gateway(
            summary=_paper_summary(),
            held=[],
            emitter=emitter,
            broker_time=_MOMENT + timedelta(seconds=1),
            skew_threshold=2.0,
        )
        _run(gw.connect())
        _run(gw.snapshot())
        assert _stages(emitter, "clock.skew") == []

    def test_naive_broker_time_does_not_gate_the_read(self):
        # Regression: a NAIVE broker_time must not raise TypeError on the naive/aware subtraction and gate
        # the read — skew is reported, never gates (ADR ⑤/⑦/⑨). The snapshot must still return.
        emitter = RecordingEmitter()
        gw = _make_gateway(
            summary=_paper_summary(),
            held=[],
            emitter=emitter,
            broker_time=(_MOMENT + timedelta(seconds=5)).replace(tzinfo=None),  # NAIVE
            skew_threshold=2.0,
        )
        _run(gw.connect())
        account = _run(gw.snapshot())  # must NOT raise
        assert account.net_liquidation == Decimal("2000.50")
        assert _stages(emitter, "clock.skew")

    def test_naive_decision_clock_does_not_gate_the_read(self):
        # Regression (second trigger): a mis-injected NAIVE decision Clock vs an aware broker time must also
        # normalize rather than raise on the snapshot path.
        emitter = RecordingEmitter()
        gw = _make_gateway(
            clock=FixedClock(_MOMENT.replace(tzinfo=None)),  # NAIVE clock
            summary=_paper_summary(),
            held=[],
            emitter=emitter,
            broker_time=_MOMENT + timedelta(seconds=5),  # aware
            skew_threshold=2.0,
        )
        _run(gw.connect())
        account = _run(gw.snapshot())  # must NOT raise
        assert account.buying_power == Decimal("8000.00")

    def test_clock_skew_seconds_normalizes_mixed_awareness(self):
        aware = _MOMENT
        naive = (_MOMENT + timedelta(seconds=5)).replace(tzinfo=None)  # a naive value 5s later
        # a naive datetime is treated as UTC on either side; the skew is 5s and it never raises.
        assert clock_skew_seconds(aware, naive) == 5.0
        assert clock_skew_seconds(naive, aware) == 5.0
        assert clock_skew_seconds(aware, None) is None


class TestConfig:
    def test_paper_defaults(self):
        cfg = IbkrConnectionConfig()
        assert (cfg.host, cfg.port) == (DEFAULT_HOST, DEFAULT_PAPER_PORT) == ("127.0.0.1", 7497)
        assert cfg.account is None

    def test_from_env_reads_overrides(self, monkeypatch):
        monkeypatch.setenv("IBKR_HOST", "10.0.0.5")
        monkeypatch.setenv("IBKR_PORT", "4002")
        monkeypatch.setenv("IBKR_CLIENT_ID", "7")
        monkeypatch.setenv("IBKR_ACCOUNT", "DU42")
        cfg = IbkrConnectionConfig.from_env()
        assert (cfg.host, cfg.port, cfg.client_id, cfg.account) == ("10.0.0.5", 4002, 7, "DU42")

    def test_from_env_blank_account_is_none(self, monkeypatch):
        monkeypatch.setenv("IBKR_ACCOUNT", "   ")  # blank -> "not configured"
        assert IbkrConnectionConfig.from_env().account is None


class TestPortConformance:
    def test_fake_satisfies_account_gateway_protocol(self):
        gw = _make_gateway(summary=_paper_summary(), held=[])
        assert isinstance(gw, AccountGateway)  # runtime_checkable structural check


# --------------------------------------------------------------------------- #
# opt-in integration test — real paper Gateway. EXCLUDED FROM CI (marked + skipif).
# Run manually with a paper TWS/Gateway up:  IBKR_INTEGRATION=1 pytest -m integration
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("IBKR_INTEGRATION"),
    reason="opt-in: needs a live paper Gateway; set IBKR_INTEGRATION=1 to run",
)
def test_real_paper_gateway_snapshot():  # pragma: no cover - manual, not run in CI
    from ibkr_trader.ibkr import IbkrAccountGateway  # lazy import: pulls ib_async only here

    gw = IbkrAccountGateway(config=IbkrConnectionConfig.from_env(), mode=Mode.PAPER)
    try:
        _run(gw.connect())
        assert gw.is_connected()
        account = _run(gw.snapshot())
        assert isinstance(account, AccountSnapshot)
        assert account.net_liquidation >= 0
        assert account.observed_at.tzinfo is not None
        for held in account.held:
            assert isinstance(held.instrument_id, int)
            if held.valuation_status is ValuationStatus.AVAILABLE:
                assert held.broker_market_value is not None
                assert held.mark_available_at is not None and held.mark_available_at.tzinfo is not None
            else:
                assert held.broker_market_value is None and held.mark_available_at is None
    finally:
        _run(gw.disconnect())
