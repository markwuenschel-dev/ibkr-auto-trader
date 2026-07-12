"""SQLite durable state: conId-keyed positions, exact daily P&L, and idempotency."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import TracebackType

from ibkr_trader.domain.models import InstrumentId

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = _REPO_ROOT / "state" / "trader.db"
DB_PATH_ENV = "IBKR_TRADER_STATE_DB"
_PNL_PREC = 50


def _resolve_db_path(path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ[DB_PATH_ENV]) if os.environ.get(DB_PATH_ENV) else DEFAULT_DB_PATH


class StateStore:
    """Lock-serialized durable state.

    Position identity is IBKR's stable ``conId``.  ``symbol`` is display metadata
    only and intentionally is not unique: broker symbols are not stable identity.
    A migration retains legacy symbol-only rows with a null ``con_id`` until a
    resolver refresh supplies the actual identity; they never enter the conId
    position cache or acquire an invented identifier.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = _resolve_db_path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # Releases before PT-4a used ``symbol TEXT PRIMARY KEY``.  Rebuild,
            # rather than ALTER, because a symbol primary key would still prohibit
            # two broker contracts whose display metadata happens to be identical.
            if self._positions_needs_migration():
                self._migrate_positions()
            else:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS positions "
                    "(row_id INTEGER PRIMARY KEY, con_id INTEGER UNIQUE, "
                    "symbol TEXT NOT NULL, quantity INTEGER NOT NULL)"
                )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS daily_pnl (day TEXT PRIMARY KEY, realized TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS order_keys (key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)"
            )

    def _positions_needs_migration(self) -> bool:
        columns = list(self._conn.execute("PRAGMA table_info(positions)"))
        if not columns:
            return False
        by_name = {row[1]: row for row in columns}
        # Current schema has an internal row key and no uniqueness constraint on
        # display symbols.  Any prior shape is migrated atomically.
        return "row_id" not in by_name or "con_id" not in by_name or by_name["symbol"][5] != 0

    def _migrate_positions(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "CREATE TABLE positions_pt4a "
                "(row_id INTEGER PRIMARY KEY, con_id INTEGER UNIQUE, "
                "symbol TEXT NOT NULL, quantity INTEGER NOT NULL)"
            )
            old_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(positions)")}
            con_id = "con_id" if "con_id" in old_columns else "NULL"
            self._conn.execute(
                "INSERT INTO positions_pt4a(con_id, symbol, quantity) "
                f"SELECT {con_id}, symbol, quantity FROM positions"
            )
            self._conn.execute("DROP TABLE positions")
            self._conn.execute("ALTER TABLE positions_pt4a RENAME TO positions")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def upsert_position(self, instrument_id: InstrumentId, symbol: str, quantity: int) -> None:
        """Set signed inventory using IBKR's stable conId, never a symbol key."""
        with self._lock:
            # A legacy row has no authoritative identity.  Once the resolver has
            # supplied this symbol's conId, replace that stale row rather than
            # leaving two apparent entries for the same historical cache record.
            self._conn.execute("DELETE FROM positions WHERE symbol=? AND con_id IS NULL", (symbol,))
            self._conn.execute(
                "INSERT INTO positions(con_id, symbol, quantity) VALUES(?, ?, ?) "
                "ON CONFLICT(con_id) DO UPDATE SET symbol=excluded.symbol, quantity=excluded.quantity",
                (int(instrument_id), symbol, int(quantity)),
            )

    def position(self, instrument_id: InstrumentId) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT quantity FROM positions WHERE con_id=?", (int(instrument_id),)
            ).fetchone()
        return int(row[0]) if row else 0

    def all_positions(self) -> dict[InstrumentId, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT con_id, quantity FROM positions WHERE con_id IS NOT NULL"
            ).fetchall()
        return {int(con_id): int(quantity) for con_id, quantity in rows}

    def instrument_id_for_symbol(self, symbol: str) -> InstrumentId | None:
        """Return a resolved conId for display metadata, if one is known."""
        with self._lock:
            row = self._conn.execute(
                "SELECT con_id FROM positions WHERE symbol=? AND con_id IS NOT NULL "
                "ORDER BY row_id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return int(row[0]) if row else None

    def symbol_for_instrument_id(self, instrument_id: InstrumentId) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT symbol FROM positions WHERE con_id=?", (int(instrument_id),)
            ).fetchone()
        return str(row[0]) if row else None

    def add_realized_pnl(self, amount: Decimal | int | str, *, day: date | None = None) -> Decimal:
        day_str = (day or datetime.now(UTC).date()).isoformat()
        delta = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT realized FROM daily_pnl WHERE day=?", (day_str,)
                ).fetchone()
                with localcontext() as ctx:
                    ctx.prec = _PNL_PREC
                    total = (Decimal(row[0]) if row else Decimal("0")) + delta
                self._conn.execute(
                    "INSERT INTO daily_pnl(day, realized) VALUES(?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET realized=excluded.realized",
                    (day_str, str(total)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return total

    def realized_pnl(self, day: date | None = None) -> Decimal:
        with self._lock:
            row = self._conn.execute(
                "SELECT realized FROM daily_pnl WHERE day=?",
                ((day or datetime.now(UTC).date()).isoformat(),),
            ).fetchone()
        return Decimal(row[0]) if row else Decimal("0")

    def record_order_key(self, key: str) -> bool:
        with self._lock:
            result = self._conn.execute(
                "INSERT OR IGNORE INTO order_keys(key, recorded_at) VALUES(?, ?)",
                (key, datetime.now(UTC).isoformat()),
            )
        return result.rowcount == 1

    def has_order_key(self, key: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM order_keys WHERE key=?", (key,)).fetchone() is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
