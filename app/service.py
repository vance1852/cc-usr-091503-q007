"""领域服务：事件状态机、放行闸门、迟到曲线重判、失效传播。

设计约定
--------
* 状态全部由 ``item_events`` 最新事件推导，业务表只追加不覆盖；
  因此扫码重传返回既有事件、设备迟到数据只触发"重新判定"，
  任何历史隔离事件都不会被抹掉。
* 隔离（quarantine）是粘性状态：只有显式返工放行 ``dequarantine`` 才能解除，
  迟到的合格曲线不能清除它。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import tx

# ---- 周期达标阈值（保温阶段） ----------------------------------------------
REQUIRED_MIN_TEMP_C = 93.0
REQUIRED_HOLD_SECONDS = 300
REQUIRED_DRYING_SECONDS = 300
# 设备侧记录时间晚于服务端收到时间超过该值即视为迟到数据
LATE_CURVE_THRESHOLD = timedelta(minutes=10)

ITEM_TYPES = {"bottle", "nipple", "cap", "rack"}


class ServiceError(Exception):
    """业务规则错误（HTTP 层映射为 4xx）。"""

    def __init__(self, code: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class Conflict(ServiceError):
    def __init__(self, detail: str):
        super().__init__("conflict", detail, status=409)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def _payload(event: sqlite3.Row | None) -> dict[str, Any]:
    if event is None:
        return {}
    return json.loads(event["payload"])


def latest_event(conn: sqlite3.Connection, item_code: str) -> sqlite3.Row | None:
    return _row(
        conn,
        "SELECT * FROM item_events WHERE item_code=? ORDER BY id DESC LIMIT 1",
        (item_code,),
    )


def is_quarantined(conn: sqlite3.Connection, item_code: str) -> bool:
    """隔离是粘性的：取最近一条隔离类事件判断。"""
    ev = _row(
        conn,
        """SELECT event_type FROM item_events
           WHERE item_code=? AND event_type IN ('quarantined','dequarantined')
           ORDER BY id DESC LIMIT 1""",
        (item_code,),
    )
    return ev is not None and ev["event_type"] == "quarantined"


def has_event(conn: sqlite3.Connection, item_code: str, types: tuple[str, ...],
              batch_id: str | None = None) -> bool:
    sql = "SELECT 1 FROM item_events WHERE item_code=? AND event_type IN (%s)" % (
        ",".join("?" * len(types))
    )
    args: list[Any] = [item_code, *types]
    if batch_id is not None:
        sql += " AND batch_id=?"
        args.append(batch_id)
    sql += " LIMIT 1"
    return _row(conn, sql, tuple(args)) is not None


def has_event_after(conn: sqlite3.Connection, item_code: str,
                    types: tuple[str, ...], after_id: int) -> bool:
    placeholders = ",".join("?" * len(types))
    return _row(
        conn,
        f"SELECT 1 FROM item_events WHERE item_code=? AND event_type IN ({placeholders}) "
        "AND id>=? LIMIT 1",
        (item_code, *types, after_id),
    ) is not None


def record_event(
    conn: sqlite3.Connection,
    item_code: str,
    event_type: str,
    idem_key: str,
    batch_id: str | None = None,
    payload: dict[str, Any] | None = None,
    event_time: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """追加事件；幂等键已存在则原样返回（扫码重传不重复推进）。

    返回 ``(event_row, created)``。
    """
    existing = _row(
        conn,
        "SELECT * FROM item_events WHERE item_code=? AND idem_key=?",
        (item_code, idem_key),
    )
    if existing is not None:
        return existing, False
    try:
        cur = conn.execute(
            """INSERT INTO item_events
                   (item_code, event_type, batch_id, payload, idem_key, event_time)
               VALUES (?,?,?,?,?,?)""",
            (
                item_code,
                event_type,
                batch_id,
                json.dumps(payload or {}, ensure_ascii=False),
                idem_key,
                event_time or _now(),
            ),
        )
    except sqlite3.IntegrityError:
        # 并发重传：幂等键已被另一事务写入
        existing = _row(
            conn,
            "SELECT * FROM item_events WHERE item_code=? AND idem_key=?",
            (item_code, idem_key),
        )
        return existing, False
    return _row(conn, "SELECT * FROM item_events WHERE id=?", (cur.lastrowid,)), True


def _quarantine(conn: sqlite3.Connection, item_code: str, reason: str,
                batch_id: str | None, source_idem: str) -> bool:
    """追加隔离事件（若尚未隔离）。返回是否新加入隔离。"""
    if is_quarantined(conn, item_code):
        return False
    record_event(
        conn, item_code, "quarantined", f"{source_idem}#quarantine",
        batch_id=batch_id, payload={"reason": reason},
    )
    return True


# ---------------------------------------------------------------------------
# 奶具登记与前段流程
# ---------------------------------------------------------------------------
def create_item(conn: sqlite3.Connection, code: str, item_type: str) -> sqlite3.Row:
    if item_type not in ITEM_TYPES:
        raise ServiceError("bad_type", f"未知奶具类型: {item_type}")
    if _row(conn, "SELECT 1 FROM items WHERE code=?", (code,)):
        raise Conflict(f"奶具已存在: {code}")
    conn.execute("INSERT INTO items (code, item_type) VALUES (?,?)", (code, item_type))
    return _row(conn, "SELECT * FROM items WHERE code=?", (code,))


def collect(conn: sqlite3.Connection, item_code: str, idem_key: str) -> dict[str, Any]:
    """回收。奶具再次周转时回收即重新开始，重传幂等。"""
    _require_item(conn, item_code)
    if _active_membership(conn, item_code) is not None:
        raise Conflict(f"奶具 {item_code} 仍在装载批次中，不能重新回收")
    _, created = record_event(conn, item_code, "collected", idem_key)
    return {"item": item_code, "advanced": created, "event": "collected"}


def split_check(
    conn: sqlite3.Connection, item_code: str, passed: bool,
    parts_complete: bool, idem_key: str, note: str = "",
) -> dict[str, Any]:
    """拆分检查 / 清洁检查；不通过或缺件立即隔离（粘性）。"""
    _require_item(conn, item_code)
    if not has_event(conn, item_code, ("collected",)):
        raise Conflict(f"奶具 {item_code} 尚未回收，不能做拆分检查")
    with tx(conn):
        _, created = record_event(
            conn, item_code, "split_check", idem_key,
            payload={"passed": passed, "parts_complete": parts_complete, "note": note},
        )
        if created and not passed:
            _quarantine(conn, item_code, "清洁检查未通过", None, idem_key)
        if created and not parts_complete:
            _quarantine(conn, item_code, "部件缺失", None, f"{idem_key}#parts")
        quarantined = is_quarantined(conn, item_code)
    return {
        "item": item_code, "advanced": created, "event": "split_check",
        "quarantined": quarantined,
    }


def prewash(conn: sqlite3.Connection, item_code: str, idem_key: str) -> dict[str, Any]:
    _require_item(conn, item_code)
    check_ev = _row(
        conn,
        """SELECT payload FROM item_events
           WHERE item_code=? AND event_type='split_check' ORDER BY id DESC LIMIT 1""",
        (item_code,),
    )
    cp = _payload(check_ev)
    if check_ev is None:
        raise Conflict(f"奶具 {item_code} 尚未通过拆分检查，不能预洗")
    if cp.get("passed") is False or cp.get("parts_complete") is False:
        raise Conflict(f"奶具 {item_code} 检查未通过或缺件，应先隔离返工，不能预洗")
    _, created = record_event(conn, item_code, "prewashed", idem_key)
    return {"item": item_code, "advanced": created, "event": "prewashed"}


def quarantine(conn: sqlite3.Connection, item_code: str, reason: str,
               idem_key: str) -> dict[str, Any]:
    _require_item(conn, item_code)
    with tx(conn):
        created = _quarantine(conn, item_code, reason, None, idem_key)
    return {"item": item_code, "advanced": created, "event": "quarantined"}


def dequarantine(conn: sqlite3.Connection, item_code: str, approver: str,
                 idem_key: str) -> dict[str, Any]:
    """返工后显式解除隔离——解除隔离的唯一途径。"""
    _require_item(conn, item_code)
    _, created = record_event(
        conn, item_code, "dequarantined", idem_key,
        payload={"approver": approver},
    )
    return {"item": item_code, "advanced": created, "event": "dequarantined"}


# ---------------------------------------------------------------------------
# 装载批次
# ---------------------------------------------------------------------------
def create_batch(conn: sqlite3.Connection, batch_id: str, device_id: str,
                 rack_code: str) -> sqlite3.Row:
    if _row(conn, "SELECT 1 FROM batches WHERE id=?", (batch_id,)):
        raise Conflict(f"批次已存在: {batch_id}")
    rack = _row(conn, "SELECT * FROM items WHERE code=?", (rack_code,))
    if rack is None:
        raise ServiceError("no_item", f"装载架不存在: {rack_code}", 404)
    if rack["item_type"] != "rack":
        raise ServiceError("not_a_rack", f"{rack_code} 不是装载架")
    with tx(conn):
        conn.execute(
            "INSERT INTO batches (id, device_id, rack_code) VALUES (?,?,?)",
            (batch_id, device_id, rack_code),
        )
        _add_member_locked(conn, batch_id, rack_code)
    return _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))


def _require_item(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = _row(conn, "SELECT * FROM items WHERE code=?", (code,))
    if row is None:
        raise ServiceError("no_item", f"奶具不存在: {code}", 404)
    return row


def _active_membership(conn: sqlite3.Connection, item_code: str) -> sqlite3.Row | None:
    return _row(
        conn,
        "SELECT * FROM batch_members WHERE item_code=? AND active=1",
        (item_code,),
    )


def _current_cycle_start(conn: sqlite3.Connection, item_code: str) -> int | None:
    """本轮周转起点：最近一次回收事件 id。"""
    row = _row(
        conn,
        "SELECT MAX(id) AS m FROM item_events WHERE item_code=? AND event_type='collected'",
        (item_code,),
    )
    return row["m"] if row and row["m"] is not None else None


def _add_member_locked(conn: sqlite3.Connection, batch_id: str,
                       item_code: str) -> bool:
    """调用方须已持写事务。返回是否真正加入。"""
    _require_item(conn, item_code)
    batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if batch is None:
        raise ServiceError("no_batch", f"批次不存在: {batch_id}", 404)
    if batch["status"] not in ("assembling",):
        raise ServiceError(
            "batch_locked", f"批次 {batch_id} 已完成装载，不能再追加奶具", 409)

    active = _active_membership(conn, item_code)
    if active is not None:
        if active["batch_id"] == batch_id:
            return False  # 已在本批次：扫码重传，幂等
        raise Conflict(
            f"奶具 {item_code} 已在装载批次 {active['batch_id']} 中，不能重复装载")

    if is_quarantined(conn, item_code):
        raise Conflict(f"奶具 {item_code} 处于隔离中，不能装载")
    cycle_start = _current_cycle_start(conn, item_code)
    if cycle_start is None:
        raise Conflict(f"奶具 {item_code} 尚未回收，不能装载")
    # 装载前须已做本轮拆分检查；未预洗允许先上架（还原"混入"场景），
    # 但闸门会以 not_prewashed 拦截，须由装载复核撤出或返工。
    check_ev = _row(
        conn,
        """SELECT payload FROM item_events
           WHERE item_code=? AND event_type='split_check' AND id>=?
           ORDER BY id DESC LIMIT 1""",
        (item_code, cycle_start),
    )
    if check_ev is None:
        raise Conflict(f"奶具 {item_code} 本轮尚未做拆分检查，不能装载")
    latest = latest_event(conn, item_code)
    if latest is not None and latest["event_type"] in ("used", "released", "recalled"):
        raise Conflict(f"奶具 {item_code} 当前状态 {latest['event_type']}，不能装载")

    try:
        conn.execute(
            "INSERT INTO batch_members (batch_id, item_code) VALUES (?,?)",
            (batch_id, item_code),
        )
    except sqlite3.IntegrityError as e:
        # 并发下由部分唯一索引兜底
        active = _active_membership(conn, item_code)
        other = active["batch_id"] if active else "?"
        raise Conflict(f"奶具 {item_code} 装载冲突，已在批次 {other}") from e
    return True


def add_member(conn: sqlite3.Connection, batch_id: str, item_code: str) -> dict[str, Any]:
    with tx(conn):
        added = _add_member_locked(conn, batch_id, item_code)
    return {"batch": batch_id, "item": item_code, "added": added}


def remove_member(conn: sqlite3.Connection, batch_id: str, item_code: str,
                  reason: str) -> dict[str, Any]:
    """装载复核时撤出问题奶具（如混入的未预洗配件），并卸载。"""
    with tx(conn):
        m = _row(
            conn,
            """SELECT * FROM batch_members
               WHERE batch_id=? AND item_code=? AND active=1""",
            (batch_id, item_code),
        )
        if m is None:
            raise ServiceError("not_member", f"{item_code} 不在批次 {batch_id}", 404)
        batch = _require_batch(conn, batch_id)
        if batch["status"] in ("released", "invalidated"):
            raise ServiceError(
                "batch_locked", f"批次 {batch_id} 已{batch['status']}，不能撤出成员", 409)
        conn.execute(
            "UPDATE batch_members SET active=0, removed_at=?, depart_status='removed' "
            "WHERE id=?",
            (_now(), m["id"]),
        )
        _, unloaded = record_event(
            conn, item_code, "unloaded", f"unloaded:{batch_id}",
            batch_id=batch_id, payload={"reason": reason},
        )
        if reason:
            _quarantine(conn, item_code, reason, batch_id, f"remove:{batch_id}")
    return {"batch": batch_id, "item": item_code, "removed": True}


def complete_loading(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    """装载完成：为每名在架成员登记 loaded 事件（重传幂等）。"""
    with tx(conn):
        batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
        if batch is None:
            raise ServiceError("no_batch", f"批次不存在: {batch_id}", 404)
        if batch["status"] != "assembling":
            return {"batch": batch_id, "advanced": False, "status": batch["status"]}
        members = conn.execute(
            "SELECT item_code FROM batch_members WHERE batch_id=? AND active=1",
            (batch_id,),
        ).fetchall()
        if not members:
            raise ServiceError("empty_batch", "空批次不能完成装载")
        for m in members:
            record_event(
                conn, m["item_code"], "loaded", f"loaded:{batch_id}",
                batch_id=batch_id,
            )
        conn.execute(
            "UPDATE batches SET status=CASE WHEN curve_received=1 THEN 'running' "
            "ELSE 'loaded' END, loaded_at=? WHERE id=?",
            (_now(), batch_id),
        )
    return {"batch": batch_id, "advanced": True, "status": "loaded"}


# ---------------------------------------------------------------------------
# 设备周期曲线（允许迟到 / 重传）
# ---------------------------------------------------------------------------
def submit_curve(
    conn: sqlite3.Connection,
    batch_id: str,
    message_id: str,
    device_id: str,
    hold_samples: list[tuple[float, float]],
    drying_seconds: int,
    event_time: str | None = None,
) -> dict[str, Any]:
    """接收设备保温阶段温度-时长曲线并绑定到批次。

    * message_id 去重：设备重传同一消息绝不重复推进；
    * 曲线在放行前后到达都有效：放行前触发重新判定，放行后不达标则判失效。
    """
    if _row(conn, "SELECT 1 FROM device_messages WHERE message_id=?", (message_id,)):
        return {"batch": batch_id, "accepted": False, "duplicate": True}
    batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if batch is None:
        raise ServiceError("no_batch", f"批次不存在: {batch_id}", 404)
    if batch["status"] == "invalidated":
        raise ServiceError("invalidated", "批次已失效，拒绝曲线", 409)
    if batch["status"] == "assembling":
        raise ServiceError(
            "not_loaded", "批次尚未完成装载，不能绑定周期曲线", 409)
    if batch["curve_received"] and batch["status"] != "released":
        # 放行前曲线为该周期的唯一事实，不允许被另一条消息覆盖
        raise Conflict(f"批次 {batch_id} 已绑定周期曲线，不能重复提交")

    if not hold_samples:
        raise ServiceError("bad_curve", "保温阶段曲线为空")
    offsets = [float(t) for t, _ in hold_samples]
    temps = [float(v) for _, v in hold_samples]
    min_temp = min(temps)
    hold_seconds = int(round(max(offsets) - min(offsets)))
    recorded_at = _now()
    event_time = event_time or recorded_at
    try:
        ev_dt = datetime.fromisoformat(event_time)
    except ValueError:
        ev_dt = datetime.now(timezone.utc)
    late = (datetime.now(timezone.utc) - ev_dt) > LATE_CURVE_THRESHOLD

    with tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO device_messages (message_id, batch_id, device_id) "
            "VALUES (?,?,?)",
            (message_id, batch_id, device_id),
        )
        conn.execute(
            """UPDATE batches SET device_id=?, curve_received=1, min_temp_c=?,
                   hold_seconds=?, drying_seconds=?, curve_payload=?,
                   curve_event_time=?, curve_recorded_at=?,
                   status=CASE WHEN status='loaded' THEN 'running' ELSE status END
               WHERE id=?""",
            (
                device_id, min_temp, hold_seconds, drying_seconds,
                json.dumps({"hold_samples": hold_samples}, ensure_ascii=False),
                event_time, recorded_at, batch_id,
            ),
        )
        result = evaluate_locked(conn, batch_id, trigger="curve_late" if late else "curve")

    # 放行后才收到/更正曲线且周期不达标 -> 周期无效，沿发放记录传播
    if batch["status"] == "released" and not result["passed"]:
        invalidate(conn, batch_id,
                   reason=f"放行后确认周期参数不达标: {'; '.join(result['reason_texts'])}")
        result = evaluate_locked(conn, batch_id, trigger="invalidate")

    return {
        "batch": batch_id, "accepted": True, "duplicate": False, "late": late,
        "min_temp_c": min_temp, "hold_seconds": hold_seconds,
        "drying_seconds": drying_seconds, **{"evaluation": result},
    }


# ---------------------------------------------------------------------------
# 干燥与封存
# ---------------------------------------------------------------------------
def mark_dried(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    batch = _require_batch(conn, batch_id)
    if batch["status"] not in ("loaded", "running", "drying"):
        raise ServiceError("bad_status", f"批次状态 {batch['status']} 不能登记干燥", 409)
    with tx(conn):
        for m in _active_members(conn, batch_id):
            record_event(conn, m["item_code"], "dried", f"dried:{batch_id}",
                         batch_id=batch_id)
        conn.execute("UPDATE batches SET status='drying' WHERE id=?", (batch_id,))
    return {"batch": batch_id, "advanced": True}


def seal_batch(conn: sqlite3.Connection, batch_id: str,
               intact: dict[str, bool] | None = None) -> dict[str, Any]:
    """封存。intact 可逐件标注；破损件立即隔离且闸门拦截。"""
    batch = _require_batch(conn, batch_id)
    if batch["status"] not in ("running", "drying", "sealing"):
        raise ServiceError(
            "bad_status", f"批次状态 {batch['status']}，须先收到周期曲线并干燥", 409)
    intact = intact or {}
    with tx(conn):
        members = _active_members(conn, batch_id)
        for m in members:
            code = m["item_code"]
            ok = bool(intact.get(code, True))
            # 幂等键含完好性结论：原样重扫不推进；新结论（如后发现破损）可追加
            record_event(
                conn, code, "sealed", f"sealed:{batch_id}:{int(ok)}",
                batch_id=batch_id, payload={"intact": ok},
            )
            if not ok:
                _quarantine(conn, code, "封存破损", batch_id, f"seal:{batch_id}")
        conn.execute("UPDATE batches SET status='sealing' WHERE id=?", (batch_id,))
    return {"batch": batch_id, "advanced": True, "members": len(members)}


def _require_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if batch is None:
        raise ServiceError("no_batch", f"批次不存在: {batch_id}", 404)
    return batch


def _active_members(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    """当前仍占用批次的成员（在架、未离场）。"""
    return conn.execute(
        """SELECT bm.*, i.item_type FROM batch_members bm
           JOIN items i ON i.code = bm.item_code
           WHERE bm.batch_id=? AND bm.active=1 ORDER BY bm.id""",
        (batch_id,),
    ).fetchall()


def _participating_members(conn: sqlite3.Connection, batch_id: str,
                           include_rack: bool = True) -> list[sqlite3.Row]:
    """批次全部参与成员（含已随批发放离场者，不含装载期撤出者）。"""
    sql = """SELECT bm.*, i.item_type FROM batch_members bm
           JOIN items i ON i.code = bm.item_code
           WHERE bm.batch_id=?
             AND (bm.depart_status IS NULL OR bm.depart_status='released'
                  OR bm.depart_status='rack_kept')"""
    if not include_rack:
        sql += " AND i.item_type != 'rack'"
    sql += " ORDER BY bm.id"
    return conn.execute(sql, (batch_id,)).fetchall()


# ---------------------------------------------------------------------------
# 放行闸门
# ---------------------------------------------------------------------------
def _member_checks(conn: sqlite3.Connection, batch_id: str,
                   member: sqlite3.Row) -> list[dict[str, str]]:
    code = member["item_code"]
    problems: list[dict[str, str]] = []

    # 本轮回收之后的检查 / 预洗才算数
    cycle_start = _current_cycle_start(conn, code) or 0
    check_ev = _row(
        conn,
        """SELECT payload FROM item_events
           WHERE item_code=? AND event_type='split_check' AND id>=?
           ORDER BY id DESC LIMIT 1""",
        (code, cycle_start),
    )
    cp = _payload(check_ev)
    if check_ev is None:
        problems.append({"code": "not_inspected", "item": code,
                         "detail": "未做拆分清洁检查"})
    else:
        if cp.get("passed") is False:
            problems.append({"code": "inspection_failed", "item": code,
                             "detail": "清洁检查未通过"})
        if cp.get("parts_complete") is False:
            problems.append({"code": "missing_parts", "item": code,
                             "detail": "部件缺失"})

    if not has_event_after(conn, code, ("prewashed",), cycle_start):
        problems.append({"code": "not_prewashed", "item": code,
                         "detail": "未经预洗"})
    if is_quarantined(conn, code):
        problems.append({"code": "quarantined", "item": code,
                         "detail": "处于隔离状态"})
    if not has_event(conn, code, ("dried",), batch_id=batch_id):
        problems.append({"code": "not_dried", "item": code, "detail": "未干燥"})

    seal_ev = _row(
        conn,
        """SELECT payload FROM item_events
           WHERE item_code=? AND event_type='sealed' AND batch_id=?
           ORDER BY id DESC LIMIT 1""",
        (code, batch_id),
    )
    if seal_ev is None:
        problems.append({"code": "not_sealed", "item": code, "detail": "未封存"})
    elif _payload(seal_ev).get("intact") is False:
        problems.append({"code": "seal_broken", "item": code, "detail": "封存破损"})
    return problems


def _cycle_checks(batch: sqlite3.Row) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    if not batch["curve_received"]:
        problems.append({"code": "cycle_params", "item": None,
                         "detail": "设备消毒周期曲线尚未上传"})
        return problems
    if batch["min_temp_c"] is None or batch["min_temp_c"] < REQUIRED_MIN_TEMP_C:
        problems.append({"code": "cycle_params", "item": None,
                         "detail": f"保温温度 {batch['min_temp_c']}℃ 低于 "
                                   f"{REQUIRED_MIN_TEMP_C}℃"})
    if batch["hold_seconds"] is None or batch["hold_seconds"] < REQUIRED_HOLD_SECONDS:
        problems.append({"code": "cycle_params", "item": None,
                         "detail": f"保温时长 {batch['hold_seconds']}s 短于 "
                                   f"{REQUIRED_HOLD_SECONDS}s"})
    if (batch["drying_seconds"] is None
            or batch["drying_seconds"] < REQUIRED_DRYING_SECONDS):
        problems.append({"code": "cycle_params", "item": None,
                         "detail": f"干燥时长 {batch['drying_seconds']}s 短于 "
                                   f"{REQUIRED_DRYING_SECONDS}s"})
    return problems


def evaluate_locked(conn: sqlite3.Connection, batch_id: str,
                    trigger: str = "manual") -> dict[str, Any]:
    """执行闸门判定并追加判定历史。调用方持事务。"""
    batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if batch is None:
        raise ServiceError("no_batch", f"批次不存在: {batch_id}", 404)

    reasons: list[dict[str, str]] = []
    if batch["status"] == "invalidated":
        reasons.append({"code": "invalidated", "item": None,
                        "detail": batch["invalidate_reason"] or "周期已被判无效"})
    if batch["status"] == "assembling":
        reasons.append({"code": "not_loaded", "item": None, "detail": "尚未完成装载"})

    for m in _active_members(conn, batch_id):
        reasons.extend(_member_checks(conn, batch_id, m))
    reasons.extend(_cycle_checks(batch))

    passed = not reasons
    conn.execute(
        "INSERT INTO release_evaluations (batch_id, passed, reasons, trigger) "
        "VALUES (?,?,?,?)",
        (batch_id, int(passed), json.dumps(reasons, ensure_ascii=False), trigger),
    )
    # 注意：判定不回翻批次流程状态。粘性隔离挂在奶具事件上，
    # 批次是否可放行完全由每次闸门重算结果决定——迟到数据触发重判，
    # 但历史隔离事件永远保留。

    return {
        "batch_id": batch_id, "passed": passed,
        "reasons": reasons, "reason_texts": [r["detail"] for r in reasons],
        "trigger": trigger,
    }


def evaluate(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    with tx(conn):
        return evaluate_locked(conn, batch_id, trigger="manual")


# ---------------------------------------------------------------------------
# 放行审批与发放
# ---------------------------------------------------------------------------
def release(conn: sqlite3.Connection, batch_id: str, approver: str,
            destination: str = "病房") -> dict[str, Any]:
    batch = _require_batch(conn, batch_id)
    if batch["status"] == "released":
        return {"batch": batch_id, "advanced": False, "status": "released"}
    if batch["status"] == "invalidated":
        raise ServiceError("invalidated", "批次已失效，禁止放行", 409)

    with tx(conn):
        result = evaluate_locked(conn, batch_id, trigger="release")
        if not result["passed"]:
            denial = "; ".join(result["reason_texts"])
            # 拒绝决定同样留痕：先提交再抛错，避免回滚抹掉审批记录
            conn.execute(
                """UPDATE batches SET release_decision='denied', release_reason=?,
                       approver=? WHERE id=?""",
                (denial, approver, batch_id),
            )
        else:
            members = _active_members(conn, batch_id)
            released_count = 0
            for m in members:
                if m["item_type"] == "rack":
                    # 装载架是消毒间器具，不随批发放到病房
                    continue
                record_event(
                    conn, m["item_code"], "released", f"released:{batch_id}",
                    batch_id=batch_id,
                    payload={"approver": approver, "destination": destination},
                )
                released_count += 1
            # 随批发放即离场：释放占用，奶具周转回收后可进入下一批次
            conn.execute(
                "UPDATE batch_members SET active=0, "
                "depart_status=CASE WHEN (SELECT item_type FROM items "
                "WHERE code=item_code)='rack' THEN 'rack_kept' ELSE 'released' END "
                "WHERE batch_id=? AND active=1",
                (batch_id,),
            )
            conn.execute(
                """UPDATE batches SET status='released', release_decision='approved',
                       release_reason=NULL, approver=?, released_at=? WHERE id=?""",
                (approver, _now(), batch_id),
            )

    if not result["passed"]:
        raise ServiceError(
            "release_denied", "放行被拒绝: " + denial, 422)
    return {"batch": batch_id, "advanced": True, "status": "released",
            "approver": approver, "destination": destination,
            "members": released_count}


def mark_used(conn: sqlite3.Connection, item_code: str, idem_key: str) -> dict[str, Any]:
    """奶具在房间被使用。已回收/隔离/追回的奶具不能登记使用。"""
    _require_item(conn, item_code)
    latest = latest_event(conn, item_code)
    if latest is None or latest["event_type"] not in ("released", "used"):
        raise Conflict(
            f"奶具 {item_code} 未处于已发放状态（当前: "
            f"{latest['event_type'] if latest else '无事件'}），不能登记使用")
    _, created = record_event(conn, item_code, "used", idem_key)
    return {"item": item_code, "advanced": created, "event": "used"}


# ---------------------------------------------------------------------------
# 放行后周期失效 -> 回收 / 核查事项
# ---------------------------------------------------------------------------
def invalidate(conn: sqlite3.Connection, batch_id: str, reason: str) -> dict[str, Any]:
    """周期事后被判无效：沿发放记录定位去向并生成事项。

    * 已发放且未使用（仍在房间）-> recall 回收；
    * 已经使用 -> investigation 后续核查；
    * 仍在本科室的在架奶具 -> 隔离，等待返工。
    已存在的隔离与事项不重复生成。
    """
    with tx(conn):
        batch = _require_batch(conn, batch_id)
        already = batch["status"] == "invalidated"
        conn.execute(
            """UPDATE batches SET status='invalidated', invalidated_at=?,
                   invalidate_reason=? WHERE id=?""",
            (_now(), reason, batch_id),
        )

        followups: list[dict[str, str]] = []
        for m in _participating_members(conn, batch_id, include_rack=True):
            code = m["item_code"]
            rel = _row(
                conn,
                """SELECT payload FROM item_events
                   WHERE item_code=? AND event_type='released' AND batch_id=?
                   ORDER BY id DESC LIMIT 1""",
                (code, batch_id),
            )
            if rel is None and m["depart_status"] is None:
                # 没发出去且仍在架：留在消毒间，隔离待返工
                _quarantine(conn, code, f"批次 {batch_id} 周期无效",
                            batch_id, f"inv:{batch_id}")
                continue
            if m["item_type"] == "rack":
                # 装载架留在消毒间复用，不进病房去向
                continue
            destination = _payload(rel).get("destination", "病房")
            rel_id_row = _row(
                conn,
                """SELECT id FROM item_events
                   WHERE item_code=? AND event_type='released' AND batch_id=?
                   ORDER BY id DESC LIMIT 1""",
                (code, batch_id),
            )
            rel_id = rel_id_row["id"]
            # 下一轮回收即终止本批去向追踪
            next_collect = _row(
                conn,
                "SELECT MIN(id) AS m FROM item_events "
                "WHERE item_code=? AND event_type='collected' AND id>?",
                (code, rel_id),
            )
            upper = next_collect["m"] if next_collect and next_collect["m"] is not None else 10**18
            used_after = _row(
                conn,
                "SELECT 1 FROM item_events WHERE item_code=? AND event_type='used' "
                "AND id>? AND id<? LIMIT 1",
                (code, rel_id, upper),
            )
            if used_after is not None:
                kind, detail = "investigation", f"已在{destination}使用，需后续核查"
            else:
                kind, detail = "recall", f"仍在{destination}，立即回收"
            cur = conn.execute(
                """INSERT OR IGNORE INTO followups
                       (batch_id, item_code, kind, detail)
                   VALUES (?,?,?,?)""",
                (batch_id, code, kind, detail),
            )
            if cur.rowcount == 1:
                followups.append({"item": code, "kind": kind, "detail": detail,
                                  "destination": destination})
            if kind == "recall":
                record_event(
                    conn, code, "recalled", f"recalled:{batch_id}",
                    batch_id=batch_id, payload={"reason": reason},
                )

        # 再次失效不抹任何历史，仅幂等补齐事项
        result = evaluate_locked(conn, batch_id, trigger="invalidate")
    return {"batch": batch_id, "invalidated": not already,
            "followups": followups, "evaluation": result}


def resolve_followup(conn: sqlite3.Connection, followup_id: int) -> dict[str, Any]:
    with tx(conn):
        row = _row(conn, "SELECT * FROM followups WHERE id=?", (followup_id,))
        if row is None:
            raise ServiceError("no_followup", f"事项不存在: {followup_id}", 404)
        conn.execute(
            "UPDATE followups SET status='done', resolved_at=? WHERE id=?",
            (_now(), followup_id),
        )
    return {"id": followup_id, "status": "done"}


# ---------------------------------------------------------------------------
# 主管批次视图
# ---------------------------------------------------------------------------
def batch_detail(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    batch = _require_batch(conn, batch_id)
    members = []
    for m in _participating_members(conn, batch_id):
        code = m["item_code"]
        latest = latest_event(conn, code)
        rel = _row(
            conn,
            """SELECT payload FROM item_events
               WHERE item_code=? AND event_type='released' AND batch_id=?
               ORDER BY id DESC LIMIT 1""",
            (code, batch_id),
        )
        members.append({
            "item": code,
            "item_type": m["item_type"],
            "state": latest["event_type"] if latest else None,
            "quarantined": is_quarantined(conn, code),
            "destination": _payload(rel).get("destination") if rel else None,
        })

    latest_eval = _row(
        conn,
        "SELECT * FROM release_evaluations WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,),
    )
    curve = None
    if batch["curve_received"]:
        late = None
        if batch["curve_event_time"] and batch["curve_recorded_at"]:
            try:
                late = (
                    datetime.fromisoformat(batch["curve_recorded_at"])
                    - datetime.fromisoformat(batch["curve_event_time"])
                ) > LATE_CURVE_THRESHOLD
            except ValueError:
                late = None
        curve = {
            "min_temp_c": batch["min_temp_c"],
            "hold_seconds": batch["hold_seconds"],
            "drying_seconds": batch["drying_seconds"],
            "device_event_time": batch["curve_event_time"],
            "received_at": batch["curve_recorded_at"],
            "late": late,
            "thresholds": {
                "min_temp_c": REQUIRED_MIN_TEMP_C,
                "hold_seconds": REQUIRED_HOLD_SECONDS,
                "drying_seconds": REQUIRED_DRYING_SECONDS,
            },
            "samples": _payload_raw(conn, batch["curve_payload"]),
        }

    followups = [
        dict(r) for r in conn.execute(
            "SELECT * FROM followups WHERE batch_id=? ORDER BY id", (batch_id,))
    ]
    removed = [
        {"item": r["item_code"], "removed_at": r["removed_at"]}
        for r in conn.execute(
            "SELECT * FROM batch_members WHERE batch_id=? AND depart_status='removed' "
            "ORDER BY id",
            (batch_id,))
    ]
    return {
        "batch_id": batch_id,
        "device_id": batch["device_id"],
        "rack": batch["rack_code"],
        "status": batch["status"],
        "loaded_at": batch["loaded_at"],
        "loading": {"members": members, "removed": removed},
        "curve": curve,
        "latest_evaluation": None if latest_eval is None else {
            "passed": bool(latest_eval["passed"]),
            "reasons": json.loads(latest_eval["reasons"]),
            "trigger": latest_eval["trigger"],
            "at": latest_eval["created_at"],
        },
        "approval": {
            "decision": batch["release_decision"],
            "approver": batch["approver"],
            "reason": batch["release_reason"],
            "released_at": batch["released_at"],
        },
        "invalidation": None if not batch["invalidated_at"] else {
            "at": batch["invalidated_at"], "reason": batch["invalidate_reason"]},
        "affected_followups": followups,
    }


def _payload_raw(_conn: sqlite3.Connection, raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)
