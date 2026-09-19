"""SQLite 存储层：连接管理与建表。

写事务统一使用 BEGIN IMMEDIATE，配合 WAL 与 busy_timeout，
保证多线程/多请求并发装载时的串行化一致性。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_code        TEXT PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('bottle','nipple','cap','rack')),
    set_id           TEXT,
    status           TEXT NOT NULL DEFAULT 'registered',
    current_batch_id INTEGER,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code  TEXT NOT NULL UNIQUE,
    device_id   TEXT NOT NULL,
    operator    TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    item_code  TEXT NOT NULL REFERENCES items(item_code),
    loaded_at  TEXT NOT NULL,
    PRIMARY KEY (batch_id, item_code)
);

CREATE TABLE IF NOT EXISTS item_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code       TEXT NOT NULL REFERENCES items(item_code),
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_item ON item_events(item_code, id);

CREATE TABLE IF NOT EXISTS cycle_uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    points      TEXT NOT NULL,
    started_at  TEXT,
    ended_at    TEXT,
    assessment  TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cycle_batch ON cycle_uploads(batch_id, id);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    decision   TEXT NOT NULL,
    reasons    TEXT NOT NULL DEFAULT '[]',
    approver   TEXT,
    trigger    TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_batch ON decisions(batch_id, id);

CREATE TABLE IF NOT EXISTS distributions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL REFERENCES batches(id),
    item_code      TEXT NOT NULL REFERENCES items(item_code),
    room           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'in_room',
    distributed_at TEXT NOT NULL,
    used_at        TEXT,
    recalled_at    TEXT,
    UNIQUE (batch_id, item_code)
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('recall','followup_check')),
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    item_code    TEXT NOT NULL,
    room         TEXT,
    detail       TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (kind, batch_id, item_code)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE，提交或回滚。

        若异常带有 commit=True（如放行被阻断时落库的隔离决定），
        则先提交已做的状态变更再把异常抛给调用方。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception as exc:
            if getattr(exc, "commit", False):
                conn.commit()
            else:
                conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
