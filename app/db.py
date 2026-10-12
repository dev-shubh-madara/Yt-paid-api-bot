"""SQLite storage.

One shared connection, and EVERY operation takes the same lock. That keeps a
multi-statement transaction from ever interleaving with another request.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, NamedTuple

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT 'default',
    plan            TEXT NOT NULL DEFAULT 'free',
    plan_expires_at INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    created_ip      TEXT,
    last_used_at    INTEGER,
    total_plays     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_owner_active ON api_keys(owner) WHERE active = 1;

CREATE TABLE IF NOT EXISTS usage (
    key_id INTEGER NOT NULL,
    day    TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);

CREATE TABLE IF NOT EXISTS orders (
    id           TEXT PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    key_id       INTEGER NOT NULL,
    plan         TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    status       TEXT NOT NULL,
    claimed_utr  TEXT,
    payment_id   INTEGER,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    paid_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_key ON orders(key_id);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL UNIQUE,
    utr          TEXT,
    amount_paise INTEGER,
    code         TEXT,
    payer        TEXT,
    received_at  INTEGER NOT NULL,
    status       TEXT NOT NULL,
    order_id     TEXT,
    note         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_utr ON payments(utr) WHERE utr IS NOT NULL;

CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   TEXT PRIMARY KEY,
    from_addr    TEXT,
    subject      TEXT,
    result       TEXT,
    processed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Result(NamedTuple):
    rowcount: int
    lastrowid: int | None


class Conn:
    """Handle used inside a transaction (the lock is already held)."""

    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn

    async def execute(self, sql: str, params: tuple | list = ()) -> Result:
        async with self._c.execute(sql, params) as cur:
            return Result(cur.rowcount, cur.lastrowid)

    async def fetchone(self, sql: str, params: tuple | list = ()) -> aiosqlite.Row | None:
        async with self._c.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[aiosqlite.Row]:
        async with self._c.execute(sql, params) as cur:
            return list(await cur.fetchall())


class Database:
    def __init__(self, path: str):
        self.path = path
        self._c: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._c = await aiosqlite.connect(self.path, isolation_level=None)
        self._c.row_factory = aiosqlite.Row
        await self._c.execute("PRAGMA journal_mode=WAL")
        await self._c.execute("PRAGMA synchronous=NORMAL")
        await self._c.execute("PRAGMA busy_timeout=5000")
        await self._c.executescript(SCHEMA)

    async def close(self) -> None:
        if self._c is not None:
            await self._c.close()
            self._c = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._c is not None, "database not connected"
        return self._c

    async def execute(self, sql: str, params: tuple | list = ()) -> Result:
        async with self._lock:
            return await Conn(self.conn).execute(sql, params)

    async def fetchone(self, sql: str, params: tuple | list = ()) -> Any:
        async with self._lock:
            return await Conn(self.conn).fetchone(sql, params)

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[Any]:
        async with self._lock:
            return await Conn(self.conn).fetchall(sql, params)

    @asynccontextmanager
    async def tx(self):
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield Conn(self.conn)
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")
