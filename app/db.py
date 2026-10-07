"""Penyimpanan SQLite (satu file, bawaan Python) untuk akun simulasi, pengaturan, log & backtest.

Satu koneksi dipakai bersama oleh semua thread (request, bot auto-trading, pemantau sinyal),
diserialkan dengan lock; mode WAL membuat pembacaan tidak terblokir penulisan.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,          -- JSON
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    broker        TEXT PRIMARY KEY,
    cash          REAL NOT NULL,
    starting_cash REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    broker     TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    shares     INTEGER NOT NULL,
    avg_price  REAL NOT NULL,
    PRIMARY KEY (broker, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
    id           TEXT PRIMARY KEY,
    broker       TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    lots         INTEGER NOT NULL,
    order_type   TEXT NOT NULL,
    limit_price  REAL,
    status       TEXT NOT NULL,
    fill_price   REAL,
    fee          REAL NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    filled_at    REAL,
    message      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS orders_broker_time ON orders (broker, created_at);
CREATE INDEX IF NOT EXISTS orders_symbol ON orders (broker, symbol);
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    stream  TEXT NOT NULL,              -- autotrader / notifications / webhook
    time    REAL NOT NULL,
    kind    TEXT NOT NULL,
    symbol  TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    extra   TEXT NOT NULL DEFAULT '{}'  -- JSON: field tambahan per jenis log
);
CREATE INDEX IF NOT EXISTS logs_stream ON logs (stream, id);
CREATE TABLE IF NOT EXISTS backtests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    symbols     TEXT NOT NULL,
    period      TEXT NOT NULL,
    total_return_pct REAL,
    result      TEXT NOT NULL           -- JSON hasil lengkap
);
"""

ORDER_COLUMNS = ("id", "symbol", "side", "lots", "order_type", "limit_price", "status", "fill_price",
                 "fee", "created_at", "filled_at", "message", "source")
LOG_COLUMNS = ("time", "kind", "symbol", "message")
# Kolom "level" dipakai log auto-trader; disimpan di kolom kind yang sama.
LOG_KIND_ALIASES = {"autotrader": "level"}


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _tx(self):
        return _Transaction(self)

    # ---- pengaturan (key -> JSON) ------------------------------------
    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value), time.time()))

    # ---- akun simulasi -----------------------------------------------
    def load_account(self, broker: str):
        """(cash, starting_cash, positions, orders) atau None bila akun belum ada."""
        with self._lock:
            acct = self.conn.execute("SELECT * FROM accounts WHERE broker = ?", (broker,)).fetchone()
            if not acct:
                return None
            positions = {r["symbol"]: {"shares": r["shares"], "avg_price": r["avg_price"]}
                         for r in self.conn.execute("SELECT * FROM positions WHERE broker = ?", (broker,))}
            orders = [{c: r[c] for c in ORDER_COLUMNS} for r in self.conn.execute(
                "SELECT * FROM orders WHERE broker = ? ORDER BY created_at, rowid", (broker,))]
        return acct["cash"], acct["starting_cash"], positions, orders

    def save_account(self, broker: str, cash: float, starting_cash: float, positions: dict,
                     orders: list[dict] = ()) -> None:
        """Simpan kas & posisi, plus upsert order yang berubah — dalam satu transaksi."""
        with self._tx() as c:
            c.execute("INSERT INTO accounts (broker, cash, starting_cash, updated_at) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(broker) DO UPDATE SET cash = excluded.cash, "
                      "starting_cash = excluded.starting_cash, updated_at = excluded.updated_at",
                      (broker, cash, starting_cash, time.time()))
            c.execute("DELETE FROM positions WHERE broker = ?", (broker,))
            c.executemany("INSERT INTO positions (broker, symbol, shares, avg_price) VALUES (?, ?, ?, ?)",
                          [(broker, s, p["shares"], p["avg_price"]) for s, p in positions.items()])
            cols = ("broker",) + ORDER_COLUMNS
            c.executemany(
                f"INSERT OR REPLACE INTO orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [(broker,) + tuple(o[k] for k in ORDER_COLUMNS) for o in orders])

    def reset_account(self, broker: str, cash: float) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM orders WHERE broker = ?", (broker,))
            c.execute("DELETE FROM positions WHERE broker = ?", (broker,))
            c.execute("INSERT OR REPLACE INTO accounts (broker, cash, starting_cash, updated_at) VALUES (?, ?, ?, ?)",
                      (broker, cash, cash, time.time()))

    # ---- log ---------------------------------------------------------
    def append_log(self, stream: str, entry: dict) -> int:
        kind_key = LOG_KIND_ALIASES.get(stream, "kind")
        extra = {k: v for k, v in entry.items() if k not in (*LOG_COLUMNS, kind_key, "id")}
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO logs (stream, time, kind, symbol, message, extra) VALUES (?, ?, ?, ?, ?, ?)",
                (stream, entry["time"], entry.get(kind_key, ""), entry.get("symbol", ""), entry.get("message", ""),
                 json.dumps(extra, default=str)))
            return cur.lastrowid

    def recent_logs(self, stream: str, limit: int = 200) -> list[dict]:
        """Log terbaru dulu, dalam bentuk yang sama seperti saat ditulis."""
        kind_key = LOG_KIND_ALIASES.get(stream, "kind")
        with self._lock:
            rows = self.conn.execute("SELECT * FROM logs WHERE stream = ? ORDER BY id DESC LIMIT ?",
                                     (stream, limit)).fetchall()
        return [{"id": r["id"], "time": r["time"], kind_key: r["kind"], "symbol": r["symbol"],
                 "message": r["message"], **json.loads(r["extra"])} for r in rows]

    def prune_logs(self, keep_days: int = 180) -> int:
        with self._lock:
            return self.conn.execute("DELETE FROM logs WHERE time < ?", (time.time() - keep_days * 86400,)).rowcount

    # ---- backtest ----------------------------------------------------
    def save_backtest(self, result: dict) -> int:
        p = result["params"]
        with self._lock:
            return self.conn.execute(
                "INSERT INTO backtests (created_at, symbols, period, total_return_pct, result) VALUES (?, ?, ?, ?, ?)",
                (time.time(), ",".join(p["symbols"]), p["period"], result["metrics"].get("total_return_pct"),
                 json.dumps(result))).lastrowid

    def list_backtests(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, created_at, symbols, period, total_return_pct, result FROM backtests "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            res = json.loads(r["result"])
            m = res["metrics"]
            out.append({"id": r["id"], "created_at": r["created_at"], "symbols": r["symbols"].split(","),
                        "period": r["period"], "execution": res["params"]["execution"],
                        "start": res["params"]["start"], "end": res["params"]["end"],
                        "total_return_pct": r["total_return_pct"], "ihsg_return_pct": m.get("ihsg_return_pct"),
                        "max_drawdown_pct": m.get("max_drawdown_pct"), "trades": m.get("trades"),
                        "strategy": res["params"]["strategy"]})
        return out

    def get_backtest(self, backtest_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT id, result FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
        return {**json.loads(row["result"]), "id": row["id"]} if row else None

    def delete_backtest(self, backtest_id: int) -> bool:
        with self._lock:
            return self.conn.execute("DELETE FROM backtests WHERE id = ?", (backtest_id,)).rowcount > 0

    # ---- migrasi dari file JSON versi lama ----------------------------
    def migrate_json(self, data_dir: Path, starting_cash: float) -> list[str]:
        """Impor file JSON lama (sekali). File asli diganti nama menjadi *.json.migrated."""
        done = []
        acct = data_dir / "paper_account.json"
        if acct.exists() and self.load_account("paper") is None:
            raw = json.loads(acct.read_text())
            orders = [{**{k: None for k in ORDER_COLUMNS}, "fee": 0.0, "message": "", "source": "manual", **o}
                      for o in raw.get("orders", [])]
            self.save_account("paper", raw["cash"], raw.get("starting_cash", starting_cash),
                              raw.get("positions", {}), orders)
            done.append(acct.name)
        for name, key in (("autotrader.json", "autotrader"), ("notifications.json", "notifications"),
                          ("webhook.json", "webhook")):
            f = data_dir / name
            if f.exists() and self.get_setting(key) is None:
                self.set_setting(key, json.loads(f.read_text()))
                done.append(name)
        for name in done:
            (data_dir / name).rename(data_dir / f"{name}.migrated")
        if done:
            log.info("Data JSON lama dipindahkan ke SQLite: %s", ", ".join(done))
        return done


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False
