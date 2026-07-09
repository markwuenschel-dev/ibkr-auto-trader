"""PT-2 state-store tests — positions cache, restart-surviving daily P&L, idempotency dedupe, idempotent init.

The load-bearing test is ``test_daily_pnl_survives_restart``: it proves the -3% daily-loss lockout still
sees the day's realized loss after the process is torn down and a fresh store reopens the same db file.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_trader.state import StateStore


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "trader.db"


class TestPositions:
    def test_upsert_and_read_signed_quantity(self, db_path):
        with StateStore(db_path) as store:
            store.upsert_position("AAPL", 10)
            store.upsert_position("MSFT", -4)  # short: signed
            assert store.position("AAPL") == 10
            assert store.position("MSFT") == -4

    def test_unknown_symbol_is_flat_zero(self, db_path):
        with StateStore(db_path) as store:
            assert store.position("NVDA") == 0

    def test_upsert_overwrites(self, db_path):
        with StateStore(db_path) as store:
            store.upsert_position("AAPL", 10)
            store.upsert_position("AAPL", 25)
            assert store.position("AAPL") == 25

    def test_all_positions(self, db_path):
        with StateStore(db_path) as store:
            store.upsert_position("AAPL", 10)
            store.upsert_position("MSFT", -4)
            assert store.all_positions() == {"AAPL": 10, "MSFT": -4}


class TestDailyPnl:
    def test_accumulates_within_a_day(self, db_path):
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            assert store.add_realized_pnl(Decimal("-12.50"), day=day) == Decimal("-12.50")
            assert store.add_realized_pnl(Decimal("-7.25"), day=day) == Decimal("-19.75")
            assert store.realized_pnl(day=day) == Decimal("-19.75")

    def test_days_are_isolated(self, db_path):
        with StateStore(db_path) as store:
            store.add_realized_pnl(Decimal("-30"), day=date(2026, 7, 6))
            store.add_realized_pnl(Decimal("-5"), day=date(2026, 7, 7))
            assert store.realized_pnl(day=date(2026, 7, 6)) == Decimal("-30")
            assert store.realized_pnl(day=date(2026, 7, 7)) == Decimal("-5")

    def test_unrecorded_day_is_zero(self, db_path):
        with StateStore(db_path) as store:
            assert store.realized_pnl(day=date(2026, 1, 1)) == Decimal("0")

    def test_money_is_exact_not_float(self, db_path):
        # 0.1 + 0.2 in float is 0.30000000000000004; Decimal TEXT storage must stay exact.
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            store.add_realized_pnl(Decimal("0.1"), day=day)
            total = store.add_realized_pnl(Decimal("0.2"), day=day)
            assert total == Decimal("0.3")

    def test_daily_pnl_survives_restart(self, db_path):
        """The point of the slice: realized loss is still there after a fresh process reopens the db."""
        day = date(2026, 7, 7)
        store1 = StateStore(db_path)
        store1.add_realized_pnl(Decimal("-45.00"), day=day)
        store1.add_realized_pnl(Decimal("-15.00"), day=day)
        store1.close()  # simulate process death

        store2 = StateStore(db_path)  # fresh process, same db file
        try:
            assert store2.realized_pnl(day=day) == Decimal("-60.00")
            # and accumulation continues on top of the recovered total
            assert store2.add_realized_pnl(Decimal("-5.00"), day=day) == Decimal("-65.00")
        finally:
            store2.close()


class TestIdempotency:
    def test_record_is_true_first_then_false(self, db_path):
        with StateStore(db_path) as store:
            assert store.record_order_key("order-abc") is True
            assert store.record_order_key("order-abc") is False  # duplicate -> no-op

    def test_distinct_keys_both_claim(self, db_path):
        with StateStore(db_path) as store:
            assert store.record_order_key("k1") is True
            assert store.record_order_key("k2") is True

    def test_has_order_key_is_read_only(self, db_path):
        with StateStore(db_path) as store:
            assert store.has_order_key("k1") is False
            assert store.record_order_key("k1") is True
            assert store.has_order_key("k1") is True

    def test_keys_survive_restart(self, db_path):
        store1 = StateStore(db_path)
        assert store1.record_order_key("k1") is True
        store1.close()

        store2 = StateStore(db_path)
        try:
            assert store2.record_order_key("k1") is False  # still deduped after restart
        finally:
            store2.close()


class TestSchemaInit:
    def test_init_is_idempotent(self, db_path):
        # Opening the same db repeatedly must not error and must preserve data.
        with StateStore(db_path) as store:
            store.upsert_position("AAPL", 7)
        with StateStore(db_path) as store:
            assert store.position("AAPL") == 7
        with StateStore(db_path) as store:  # third open, still fine
            assert store.position("AAPL") == 7

    def test_default_path_env_override(self, tmp_path, monkeypatch):
        from ibkr_trader.state import DB_PATH_ENV

        target = tmp_path / "nested" / "custom.db"
        monkeypatch.setenv(DB_PATH_ENV, str(target))
        with StateStore() as store:  # no explicit path -> env override wins, dir auto-created
            store.upsert_position("AAPL", 3)
        assert target.exists()


class TestConcurrencyAndPrecision:
    """Regression tests for the round-2 adversarial-lane findings (threading + Decimal precision)."""

    def test_connection_is_usable_across_threads(self, db_path):
        # Regression: the store advertises lock-serialized sharing; a call from another OS thread must not
        # raise sqlite3.ProgrammingError (sqlite3's default check_same_thread=True would). See store.py.
        import threading

        store = StateStore(db_path)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                store.upsert_position("AAPL", 5)
                store.add_realized_pnl(Decimal("-1.00"), day=date(2026, 7, 7))
            except Exception as e:  # capture; assert on the main thread
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        try:
            assert errors == []
            assert store.position("AAPL") == 5
        finally:
            store.close()

    def test_accumulation_is_exact_beyond_default_precision(self, db_path):
        # Regression: the default 28-digit Decimal context would ROUND this sum; a money ledger must not.
        day = date(2026, 1, 1)
        with StateStore(db_path) as store:
            store.add_realized_pnl(Decimal("10000000000000000000000000000"), day=day)  # 1e28, 29 digits
            total = store.add_realized_pnl(Decimal("1"), day=day)
            assert total == Decimal("10000000000000000000000000001")
