"""store — the SQLite state store (PT-2): positions cache, restart-surviving daily P&L, idempotency keys.

This is the *durable* memory the control plane leans on between process restarts. Its single load-bearing
job: the -3% daily-loss lockout (config.RiskLimits.daily_loss_lockout) must hold even if the process dies
mid-session and restarts from another working directory — so realized P&L is persisted per UTC trading day
in a repo-anchored SQLite file, not recomputed from an in-memory tally that resets to zero on boot.

Design (reviewer decisions, handoff 027):
  * stdlib ``sqlite3`` only — no ORM. WAL + ``synchronous=FULL`` for durability; schema is idempotent.
  * Money is stored as TEXT (exact ``Decimal`` round-trip), never float.
  * ``add_realized_pnl`` is the restart-surviving source of truth for the day's realized P&L: it is a
    lost-update-safe accumulate, done under an explicit ``BEGIN IMMEDIATE`` read-modify-write.
  * The store is POLICY-FREE about what "today" means — ``day`` defaults to ``datetime.now(UTC).date()``
    but is injectable; the US/Eastern session-boundary policy lives in the Risk layer, which will pass the
    canonical session date.
  * Idempotency is CLAIM-ON-WRITE: ``record_order_key`` returns True only on the first insert. Order
    submission MUST branch on that bool — ``has_order_key`` is a read-only convenience, not the dedupe
    primitive (a separate check-then-act is a TOCTOU race).

No broker calls here — pure persistence, PAPER-first.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import TracebackType

# store.py lives at <repo>/src/ibkr_trader/state/store.py — parents[3] is the repo root. Anchoring the
# default to the repo (not the CWD) is deliberate: a CWD-keyed lockup ledger would silently reset when the
# process restarts from a different directory, defeating the -3% lockout exactly when it matters most.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = _REPO_ROOT / "state" / "trader.db"

#: Env override for the db path (absolute or relative). Empty/unset -> DEFAULT_DB_PATH.
DB_PATH_ENV = "IBKR_TRADER_STATE_DB"

#: Decimal precision for P&L accumulation. The default 28-digit context would ROUND sums needing more
#: significant digits (a money ledger must not); realistic daily realized P&L is far under 20 digits, so
#: this leaves a very wide exactness margin.
_PNL_PREC = 50


def _resolve_db_path(path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(DB_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


class StateStore:
    """Durable SQLite state for one trading process. Safe to share across threads: one connection
    (opened ``check_same_thread=False``) with a lock serializing every access — the lock is what makes
    the shared connection safe, not the default single-thread affinity.

    Open it once and reuse it; call :meth:`close` (or use it as a context manager) when done. Reopening a
    new ``StateStore`` on the same db file recovers all persisted state — that is the restart contract.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = _resolve_db_path(path)
        # ``:memory:`` has no parent to create; a real file needs its directory to exist.
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # isolation_level=None -> autocommit mode: we drive transactions explicitly (BEGIN IMMEDIATE)
        # where read-modify-write atomicity matters, and rely on single-statement autocommit elsewhere.
        # check_same_thread=False: every method serializes access through self._lock, so sharing the one
        # connection across threads (e.g. an asyncio executor thread) is safe. The default True would raise
        # sqlite3.ProgrammingError on any cross-thread use despite the lock.
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        cur = self._conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")

    def _init_schema(self) -> None:
        # ``IF NOT EXISTS`` makes init idempotent — reopening an existing db is a no-op here.
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS positions ("
                "  symbol TEXT PRIMARY KEY,"
                "  quantity INTEGER NOT NULL"  # signed shares
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS daily_pnl ("
                "  day TEXT PRIMARY KEY,"  # ISO date, e.g. '2026-07-07'
                "  realized TEXT NOT NULL"  # Decimal as TEXT — exact, never float
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS order_keys ("
                "  key TEXT PRIMARY KEY,"
                "  recorded_at TEXT NOT NULL"  # ISO-8601 UTC timestamp
                ")"
            )

    # ----------------------------------------------------------------- positions
    def upsert_position(self, symbol: str, quantity: int) -> None:
        """Set the cached signed share quantity for ``symbol`` (insert or replace)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO positions(symbol, quantity) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity",
                (symbol, int(quantity)),
            )

    def position(self, symbol: str) -> int:
        """Signed share quantity for ``symbol``; 0 if we hold none (never None — flat == 0)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT quantity FROM positions WHERE symbol=?", (symbol,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def all_positions(self) -> dict[str, int]:
        """All cached positions as ``symbol -> signed shares``."""
        with self._lock:
            rows = self._conn.execute("SELECT symbol, quantity FROM positions").fetchall()
        return {sym: int(qty) for sym, qty in rows}

    # --------------------------------------------------------------- daily P&L
    def add_realized_pnl(self, amount: Decimal | int | str, *, day: date | None = None) -> Decimal:
        """Accumulate ``amount`` into the given trading day's realized P&L and return the new total.

        Lost-update-safe: the read-modify-write runs inside an explicit ``BEGIN IMMEDIATE`` so two
        concurrent accumulations cannot both read the same prior value and clobber each other. This total
        is the restart-surviving source of truth for the -3% daily-loss lockout.
        """
        day_str = (day or datetime.now(UTC).date()).isoformat()
        delta = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT realized FROM daily_pnl WHERE day=?", (day_str,)
                ).fetchone()
                current = Decimal(row[0]) if row is not None else Decimal("0")
                # Accumulate under a wide-precision context: the default 28-digit context would ROUND a sum
                # that needs more significant digits, and a money ledger must stay exact.
                with localcontext() as ctx:
                    ctx.prec = _PNL_PREC
                    new_total = current + delta
                self._conn.execute(
                    "INSERT INTO daily_pnl(day, realized) VALUES(?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET realized=excluded.realized",
                    (day_str, str(new_total)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return new_total

    def realized_pnl(self, day: date | None = None) -> Decimal:
        """The persisted realized P&L for ``day`` (default: today UTC); Decimal('0') if none recorded."""
        day_str = (day or datetime.now(UTC).date()).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT realized FROM daily_pnl WHERE day=?", (day_str,)
            ).fetchone()
        return Decimal(row[0]) if row is not None else Decimal("0")

    # ------------------------------------------------------------ idempotency
    def record_order_key(self, key: str) -> bool:
        """Claim an idempotency key. Returns True only on the FIRST insert; False if already recorded.

        This is the dedupe primitive: order submission MUST branch on the return value (submit iff True).
        The INSERT OR IGNORE + rowcount claim is atomic, so it is safe against concurrent duplicate
        submissions in a way a separate ``has_order_key`` check-then-act is NOT (TOCTOU race).
        """
        recorded_at = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO order_keys(key, recorded_at) VALUES(?, ?)",
                (key, recorded_at),
            )
            return cur.rowcount == 1

    def has_order_key(self, key: str) -> bool:
        """Read-only convenience: whether ``key`` was already recorded. NOT a dedupe primitive — use
        :meth:`record_order_key` to claim-and-branch atomically."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM order_keys WHERE key=?", (key,)
            ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- lifecycle
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
