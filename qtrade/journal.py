"""Append-only trade journal (SQLite): the idempotency record, the audit trail and the forward-test data in one file.

Every signal is written BEFORE anything is sent to a broker. A webhook retried, replayed or fired twice carries the same id and
is dropped by the primary key, so an order can never be placed twice for one signal.
"""
from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, received_ms INTEGER, created_ms INTEGER, symbol TEXT, side TEXT, strategy TEXT,
  sl_dist REAL, tp_dist REAL, bid REAL, ask REAL,
  status TEXT,             -- received | rejected | placed | failed
  reasons TEXT, volume REAL, risk_usd REAL, req_price REAL, fill_price REAL,
  slippage REAL,           -- price units, positive = worse than the decision price
  latency_ms INTEGER,      -- signal created -> fill confirmed
  ticket INTEGER, code TEXT
);
CREATE TABLE IF NOT EXISTS closes (
  ticket INTEGER PRIMARY KEY, signal_id TEXT, close_ms INTEGER, pnl_usd REAL, risk_usd REAL, r REAL, exit_price REAL
);
"""


class Journal:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")  # the health endpoint can read while the worker writes
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def claim(self, sig, received_ms: int) -> bool:
        """True if this signal id is new (and is now recorded); False for a duplicate."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO signals (id, received_ms, created_ms, symbol, side, strategy, sl_dist, tp_dist, status)"
                " VALUES (?,?,?,?,?,?,?,?, 'received')",
                (sig.id, received_ms, sig.created_ms, sig.symbol, sig.side.value, sig.strategy, sig.sl_dist, sig.tp_dist),
            )
            return cur.rowcount == 1

    def update(self, signal_id: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE signals SET {cols} WHERE id = ?", (*fields.values(), signal_id))

    def add_close(self, ticket: int, signal_id: str, close_ms: int, pnl_usd: float, risk_usd: float, exit_price: float) -> None:
        r = pnl_usd / risk_usd if risk_usd > 0 else 0.0
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO closes VALUES (?,?,?,?,?,?,?)",
                             (ticket, signal_id, close_ms, pnl_usd, risk_usd, r, exit_price))

    def open_tickets(self) -> dict[int, tuple[str, float]]:
        """Placed and not yet closed: ticket -> (signal id, risk in USD). Rebuilds the router's memory after a restart."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ticket, id, risk_usd FROM signals WHERE status = 'placed' AND ticket NOT IN (SELECT ticket FROM closes)"
            ).fetchall()
        return {t: (i, r) for t, i, r in rows}

    def trades(self) -> list[dict]:
        """Closed trades with their entry data, by entry time: the forward-test sample."""
        with self._lock:
            cur = self._db.execute(
                "SELECT s.received_ms, s.symbol, s.side, s.strategy, s.sl_dist, s.bid, s.ask, s.volume, s.slippage, s.latency_ms,"
                " s.risk_usd, c.close_ms, c.pnl_usd, c.r"
                " FROM closes c JOIN signals s ON s.id = c.signal_id ORDER BY s.received_ms"
            )
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT status, COUNT(*) FROM signals GROUP BY status").fetchall())

    def reasons(self) -> dict[str, int]:
        """How often each risk check rejected a signal (one signal can fail several)."""
        out: dict[str, int] = {}
        with self._lock:
            for (rs,) in self._db.execute("SELECT reasons FROM signals WHERE status = 'rejected'"):
                for x in (rs or "").split(","):
                    if x:
                        out[x] = out.get(x, 0) + 1
        return out
