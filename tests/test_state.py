"""PT-2 state-store tests, including the PT-4a conId migration."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from decimal import Decimal

import pytest

from ibkr_trader.state import StateStore


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "trader.db"


class TestPositions:
    def test_round_trip_is_keyed_by_con_id_with_symbol_lookup(self, db_path):
        with StateStore(db_path) as store:
            store.upsert_position(265598, "AAPL", 10)
            store.upsert_position(272093, "MSFT", -4)
            assert store.position(265598) == 10
            assert store.position(272093) == -4
            assert store.all_positions() == {265598: 10, 272093: -4}
            assert store.instrument_id_for_symbol("AAPL") == 265598
            assert store.symbol_for_instrument_id(272093) == "MSFT"

    def test_unknown_instrument_is_flat_zero(self, db_path):
        with StateStore(db_path) as store:
            assert store.position(123) == 0
            assert store.instrument_id_for_symbol("NVDA") is None

    def test_upsert_overwrites_by_con_id(self, db_path):
        with StateStore(db_path) as store:
            store.upsert_position(265598, "AAPL", 10)
            store.upsert_position(265598, "AAPL", 25)
            assert store.position(265598) == 25

    def test_old_symbol_schema_rows_are_preserved_without_inventing_con_id(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL)")
        conn.execute("INSERT INTO positions VALUES ('AAPL', 7)")
        conn.commit()
        conn.close()

        with StateStore(db_path) as store:
            assert store.all_positions() == {}
            assert store.instrument_id_for_symbol("AAPL") is None
            # The old row remains until a resolver supplies its actual conId.
            store.upsert_position(265598, "AAPL", 7)
            assert store.position(265598) == 7
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT quantity FROM positions WHERE symbol='AAPL'").fetchone() == (7,)
        conn.close()


class TestDailyPnl:
    def test_accumulates_exactly_and_survives_restart(self, db_path):
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            assert store.add_realized_pnl(Decimal("-12.50"), day=day) == Decimal("-12.50")
            assert store.add_realized_pnl(Decimal("-7.25"), day=day) == Decimal("-19.75")
        with StateStore(db_path) as store:
            assert store.realized_pnl(day) == Decimal("-19.75")

    def test_wide_precision_is_exact(self, db_path):
        with StateStore(db_path) as store:
            store.add_realized_pnl(Decimal("10000000000000000000000000000"), day=date(2026, 1, 1))
            assert store.add_realized_pnl(Decimal("1"), day=date(2026, 1, 1)) == Decimal(
                "10000000000000000000000000001"
            )


class TestSessionEquity:
    def test_is_absent_until_captured_and_fails_closed(self, db_path):
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            assert store.session_start_equity(day) is None
            # A risk consumer must not turn an absent baseline into a ratio.
            baseline = store.session_start_equity(day)
            assert baseline is None

    def test_is_insert_if_absent_and_survives_restart(self, db_path):
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            assert store.set_session_start_equity(day, Decimal("10000.00")) == Decimal("10000.00")
            assert store.set_session_start_equity(day, Decimal("1")) == Decimal("10000.00")
        with StateStore(db_path) as store:
            assert store.session_start_equity(day) == Decimal("10000.00")

    def test_realized_pnl_and_baseline_accept_the_same_session_key(self, db_path):
        day = date(2026, 7, 7)
        with StateStore(db_path) as store:
            store.set_session_start_equity(day, "2500")
            store.add_realized_pnl("-25", day=day)
            assert store.session_start_equity(day) is not None
            assert store.realized_pnl(day) == Decimal("-25")


class TestIdempotencyAndConcurrency:
    def test_claim_is_first_write_wins_and_survives_restart(self, db_path):
        with StateStore(db_path) as store:
            assert store.record_order_key("k1")
            assert not store.record_order_key("k1")
        with StateStore(db_path) as store:
            assert store.has_order_key("k1")

    def test_connection_is_usable_across_threads(self, db_path):
        store = StateStore(db_path)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                store.upsert_position(265598, "AAPL", 5)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        try:
            assert errors == []
            assert store.position(265598) == 5
        finally:
            store.close()
