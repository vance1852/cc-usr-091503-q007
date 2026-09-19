"""SQLite 连接与建表脚本。

所有业务表只做追加（事件 / 判定 / 事项），状态永远由最新一条事件推导，
这样设备数据迟到、扫码重传都不会抹掉历史与已采取的隔离。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 奶具主数据：奶瓶 / 奶嘴 / 盖件 / 装载架，每件全局唯一身份
CREATE TABLE IF NOT EXISTS items (
    code        TEXT PRIMARY KEY,          -- 扫码身份码
    item_type   TEXT NOT NULL CHECK (item_type IN ('bottle','nipple','cap','rack')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 追加式事件流：一台奶具一生发生的所有事，按 seq 单调推进
CREATE TABLE IF NOT EXISTS item_events (
    id          INTEGER PRIMARY KEY,
    item_code   TEXT NOT NULL REFERENCES items(code),
    event_type  TEXT NOT NULL CHECK (event_type IN (
                    'collected','split_check','prewashed',
                    'loaded','unloaded','dried','sealed','released','used',
                    'quarantined','dequarantined','recalled')),
    batch_id    TEXT,                      -- 事件所属装载批次（若有）
    payload     TEXT NOT NULL DEFAULT '{}',-- 结构化明细（JSON）
    event_time  TEXT NOT NULL DEFAULT (datetime('now')),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 扫码重传幂等：同奶具同事件同批次（含 NULL）的同一幂等键只接受一次
    idem_key    TEXT NOT NULL,
    UNIQUE (item_code, idem_key)
);

-- 装载批次（一次设备消毒周期对应一个装载批次）
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    rack_code       TEXT REFERENCES items(code),
    status          TEXT NOT NULL DEFAULT 'assembling' CHECK (status IN (
                        'assembling','loaded','running','drying','sealing',
                        'quarantined','rejected','released','invalidated')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    loaded_at       TEXT,
    -- 设备周期曲线
    curve_received  INTEGER NOT NULL DEFAULT 0,
    min_temp_c      REAL,
    hold_seconds    INTEGER,
    drying_seconds  INTEGER,
    curve_payload   TEXT,                   -- 原始温度/时长采样 (JSON)
    curve_event_time TEXT,                  -- 设备侧记录时间（可能迟到）
    curve_recorded_at TEXT,                 -- 服务端收到时间
    -- 放行审批
    release_decision TEXT CHECK (release_decision IN ('approved','denied')),
    release_reason   TEXT,
    approver         TEXT,
    released_at      TEXT,
    -- 失效
    invalidated_at  TEXT,
    invalidate_reason TEXT
);

-- 批次成员（装载明细）。active=1 表示"当前仍在该批次中"。
-- 部分唯一索引保证同一奶具任意时刻只能在一个未完成批次里。
CREATE TABLE IF NOT EXISTS batch_members (
    id          INTEGER PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    item_code   TEXT NOT NULL REFERENCES items(code),
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at  TEXT,
    depart_status TEXT,                      -- 'removed'（装载期撤出）/ 'released'（随批发放）
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_membership
    ON batch_members(item_code) WHERE active = 1;

-- 批次闸门判定历史（每次重判追加一行，永不覆盖）
CREATE TABLE IF NOT EXISTS release_evaluations (
    id          INTEGER PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    passed      INTEGER NOT NULL,
    reasons     TEXT NOT NULL DEFAULT '[]',  -- 未通过原因 JSON 数组
    trigger     TEXT NOT NULL,              -- load / curve_late / manual / invalidate
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 设备消息去重（同一设备消息重传不产生重复推进）
CREATE TABLE IF NOT EXISTS device_messages (
    message_id  TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    device_id   TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 失效后生成的后续事项：奶具在房间 -> 回收；已使用 -> 核查
CREATE TABLE IF NOT EXISTS followups (
    id          INTEGER PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    item_code   TEXT NOT NULL REFERENCES items(code),
    kind        TEXT NOT NULL CHECK (kind IN ('recall','investigation')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    UNIQUE (batch_id, item_code, kind)      -- 重复失效不重复建事项
);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    # autocommit 模式：事务全部由 tx() 显式 BEGIN IMMEDIATE 管理，
    # 单条语句立即落盘，避免驱动隐式事务悬挂。
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 让并发装载在事务阻塞下仍然串行化通过唯一索引仲裁
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即开始的写事务（BEGIN IMMEDIATE），配合唯一索引做并发仲裁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
