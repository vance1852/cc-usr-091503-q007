"""奶具清洗消毒批次放行 —— 业务逻辑层。

所有写函数都在调用方提供的事务连接上执行（见 db.Database.tx），
并发互斥靠 BEGIN IMMEDIATE + 条件 UPDATE 的行数检查保证。

关键规则：
- 同一奶具不能同时处于两个装载批次（current_batch_id 原子占位）。
- 扫码事件按 idempotency_key 去重，重传不会重复推进状态。
- 部件缺失 / 清洁检查未通过 / 周期参数不达标 / 封存破损 -> 不得放行，批次隔离。
- 隔离是 sticky 的：迟到的设备数据只触发重新判定并留痕，不能抹掉隔离。
- 放行后发现周期无效 -> 沿发放记录生成回收（在房间）与后续核查（已使用）事项。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

HOLD_THRESHOLD_C = 90.0   # 保温阶段温度阈值 (°C)
REQUIRED_HOLD_S = 600.0   # 最短连续保温时长 (秒)

ITEM_KINDS = ("bottle", "nipple", "cap", "rack")

# 扫码事件状态机：事件类型 -> 允许的前置状态集合
EVENT_TRANSITIONS = {
    "collected": {"registered", "quarantined"},  # 隔离品可重新回收进入再处理
    "disassembly_check": {"collected", "parts_missing"},
    "prewash": {"checked", "clean_failed"},
    "clean_check": {"prewashed"},
    "dried": {"disinfected"},
    "sealed": {"dried", "seal_broken"},
}
# 由系统流程（装载/周期/放行/发放/隔离/回收）驱动的事件，不开放给扫码接口
SYSTEM_EVENTS = ("loaded", "disinfected", "released", "distributed",
                 "used", "quarantined", "recalled")


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None,
                 commit: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        # True 时 db.tx 会先提交本事务已做的变更（如阻断放行时落库的隔离）再抛出
        self.commit = commit


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------- 基础读取

def get_item(conn, item_code: str):
    row = conn.execute("SELECT * FROM items WHERE item_code=?", (item_code,)).fetchone()
    if row is None:
        raise ServiceError(404, "ITEM_NOT_FOUND", f"奶具 {item_code} 不存在")
    return row


def get_batch(conn, batch_code: str):
    row = conn.execute("SELECT * FROM batches WHERE batch_code=?", (batch_code,)).fetchone()
    if row is None:
        raise ServiceError(404, "BATCH_NOT_FOUND", f"批次 {batch_code} 不存在")
    return row


def _batch_items(conn, batch_id: int):
    return conn.execute(
        "SELECT bi.item_code, bi.loaded_at, i.kind, i.set_id, i.status "
        "FROM batch_items bi JOIN items i ON i.item_code = bi.item_code "
        "WHERE bi.batch_id=? ORDER BY bi.rowid",
        (batch_id,),
    ).fetchall()


def _latest_payloads(conn, item_code: str) -> dict:
    """该奶具每类事件的最新 payload。"""
    rows = conn.execute(
        "SELECT event_type, payload FROM item_events WHERE item_code=? ORDER BY id",
        (item_code,),
    ).fetchall()
    latest = {}
    for r in rows:
        latest[r["event_type"]] = json.loads(r["payload"])
    return latest


def _record_event_row(conn, item_code, event_type, payload, key):
    conn.execute(
        "INSERT INTO item_events(item_code, event_type, payload, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?)",
        (item_code, event_type, _j(payload or {}), key, now_iso()),
    )


def _sys_event(conn, item_code, event_type, batch_code, payload=None):
    """系统流程产生的事件，键天然唯一，重复执行不会重复记录。"""
    conn.execute(
        "INSERT OR IGNORE INTO item_events(item_code, event_type, payload, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?)",
        (item_code, event_type, _j(payload or {}),
         f"sys:{batch_code}:{item_code}:{event_type}", now_iso()),
    )


def _record_decision(conn, batch_id, decision, reasons, approver, trigger, note=None):
    conn.execute(
        "INSERT INTO decisions(batch_id, decision, reasons, approver, trigger, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (batch_id, decision, _j(reasons), approver, trigger, note, now_iso()),
    )


# ---------------------------------------------------------------- 奶具与扫码事件

def register_item(conn, item_code: str, kind: str, set_id: str | None):
    if kind not in ITEM_KINDS:
        raise ServiceError(400, "BAD_KIND", f"未知奶具类型 {kind}，应为 {ITEM_KINDS}")
    try:
        conn.execute(
            "INSERT INTO items(item_code, kind, set_id, status, created_at) VALUES (?,?,?,'registered',?)",
            (item_code, kind, set_id, now_iso()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ServiceError(409, "ITEM_EXISTS", f"奶具 {item_code} 已注册")
        raise
    return _row_to_dict(get_item(conn, item_code))


def record_event(conn, item_code: str, event_type: str, payload: dict, idempotency_key: str):
    """记录扫码事件并推进状态机。同一 idempotency_key 重传 -> 返回原事件，不重复推进。"""
    if event_type in SYSTEM_EVENTS:
        raise ServiceError(400, "SYSTEM_EVENT", f"{event_type} 由系统流程产生，不能扫码上报")
    if event_type not in EVENT_TRANSITIONS:
        raise ServiceError(400, "UNKNOWN_EVENT", f"未知事件类型 {event_type}")

    existing = conn.execute(
        "SELECT * FROM item_events WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    if existing is not None:
        if existing["item_code"] != item_code or existing["event_type"] != event_type:
            raise ServiceError(409, "KEY_REUSED",
                               f"幂等键 {idempotency_key} 已被 {existing['item_code']} 的 "
                               f"{existing['event_type']} 事件占用")
        return _row_to_dict(existing), True

    item = get_item(conn, item_code)
    allowed = EVENT_TRANSITIONS[event_type]
    if item["status"] not in allowed:
        raise ServiceError(
            409, "BAD_TRANSITION",
            f"奶具当前状态 {item['status']} 不允许 {event_type}（需 {sorted(allowed)} 之一）")

    payload = dict(payload or {})
    if event_type == "disassembly_check":
        if not isinstance(payload.get("parts_complete"), bool):
            raise ServiceError(400, "BAD_PAYLOAD", "disassembly_check 需要 parts_complete: bool")
        new_status = "checked" if payload["parts_complete"] else "parts_missing"
    elif event_type == "clean_check":
        if payload.get("result") not in ("pass", "fail"):
            raise ServiceError(400, "BAD_PAYLOAD", "clean_check 需要 result: pass|fail")
        new_status = "cleaned" if payload["result"] == "pass" else "clean_failed"
    elif event_type == "sealed":
        if not isinstance(payload.get("seal_intact"), bool):
            raise ServiceError(400, "BAD_PAYLOAD", "sealed 需要 seal_intact: bool")
        new_status = "sealed" if payload["seal_intact"] else "seal_broken"
    else:
        new_status = {"collected": "collected", "prewash": "prewashed",
                      "dried": "dried"}[event_type]

    _record_event_row(conn, item_code, event_type, payload, idempotency_key)
    conn.execute("UPDATE items SET status=? WHERE item_code=?", (new_status, item_code))
    row = conn.execute(
        "SELECT * FROM item_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return _row_to_dict(row), False


# ---------------------------------------------------------------- 批次与装载

def create_batch(conn, batch_code: str, device_id: str, operator: str | None):
    try:
        conn.execute(
            "INSERT INTO batches(batch_code, device_id, operator, status, created_at, updated_at) "
            "VALUES (?,?,?,'open',?,?)",
            (batch_code, device_id, operator, now_iso(), now_iso()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ServiceError(409, "BATCH_EXISTS", f"批次 {batch_code} 已存在")
        raise
    return _row_to_dict(get_batch(conn, batch_code))


def load_item(conn, batch_code: str, item_code: str):
    """把奶具装入批次。原子占位保证同一奶具不会同时进入两个批次。"""
    batch = get_batch(conn, batch_code)
    if batch["status"] != "open":
        raise ServiceError(409, "BATCH_NOT_OPEN",
                           f"批次状态 {batch['status']}，只有 open 状态可以装载")
    item = get_item(conn, item_code)

    already = conn.execute(
        "SELECT 1 FROM batch_items WHERE batch_id=? AND item_code=?",
        (batch["id"], item_code)).fetchone()
    if already:
        return {"batch_code": batch_code, "item_code": item_code, "deduplicated": True}

    cur = conn.execute(
        "UPDATE items SET status='loaded', current_batch_id=? "
        "WHERE item_code=? AND current_batch_id IS NULL AND status='cleaned'",
        (batch["id"], item_code))
    if cur.rowcount == 0:
        if item["current_batch_id"] is not None:
            other = conn.execute("SELECT batch_code FROM batches WHERE id=?",
                                 (item["current_batch_id"],)).fetchone()
            raise ServiceError(
                409, "ITEM_ALREADY_IN_BATCH",
                f"奶具 {item_code} 已在批次 {other['batch_code'] if other else item['current_batch_id']} 中，"
                "不能同时处于两个装载批次")
        raise ServiceError(
            409, "ITEM_NOT_READY",
            f"奶具 {item_code} 状态为 {item['status']}，需完成预洗并通过清洁检查(cleaned)才能装载")

    conn.execute("INSERT INTO batch_items(batch_id, item_code, loaded_at) VALUES (?,?,?)",
                 (batch["id"], item_code, now_iso()))
    _sys_event(conn, item_code, "loaded", batch_code, {"batch_code": batch_code})
    return {"batch_code": batch_code, "item_code": item_code, "deduplicated": False}


# ---------------------------------------------------------------- 周期曲线与判定

def assess_cycle(points, threshold: float = HOLD_THRESHOLD_C,
                 required: float = REQUIRED_HOLD_S) -> dict:
    """评估温度曲线：最长连续 >=threshold 的时长是否 >= required（线性插值越界点）。"""
    pts = sorted(((float(p["t"]), float(p["temp"])) for p in points), key=lambda x: x[0])
    base = {"threshold_c": threshold, "required_hold_s": required}
    if len(pts) < 2:
        return {**base, "passed": False, "longest_hold_s": 0.0,
                "max_temp": pts[0][1] if pts else None,
                "reason": "曲线数据点不足（至少 2 个）"}
    max_temp = max(t for _, t in pts)
    longest = 0.0
    run_start = None
    for i, (t, temp) in enumerate(pts):
        if temp >= threshold:
            if run_start is None:
                if i > 0:
                    t0, temp0 = pts[i - 1]
                    frac = (threshold - temp0) / (temp - temp0)
                    run_start = t0 + frac * (t - t0)
                else:
                    run_start = t
        elif run_start is not None:
            t0, temp0 = pts[i - 1]
            frac = (threshold - temp0) / (temp - temp0)
            longest = max(longest, t0 + frac * (t - t0) - run_start)
            run_start = None
    if run_start is not None:
        longest = max(longest, pts[-1][0] - run_start)
    passed = longest >= required
    reason = ("达标" if passed else
              f"最长连续保温 {longest:.0f}s 低于要求的 {required:.0f}s（阈值 {threshold}°C）")
    return {**base, "passed": passed, "longest_hold_s": round(longest, 1),
            "max_temp": max_temp, "reason": reason}


def evaluate_batch(conn, batch_id: int, for_release: bool) -> list:
    """汇总放行阻断原因。for_release=False 用于迟到数据复核（不做状态前置检查）。"""
    reasons = []
    cyc = conn.execute(
        "SELECT * FROM cycle_uploads WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,)).fetchone()
    if cyc is None:
        reasons.append({"rule": "CYCLE_DATA_MISSING", "detail": "设备周期曲线尚未上传"})
    else:
        assessment = json.loads(cyc["assessment"])
        if not assessment["passed"]:
            reasons.append({"rule": "CYCLE_PARAMS_OUT_OF_SPEC",
                            "detail": assessment["reason"]})

    items = _batch_items(conn, batch_id)
    if not items:
        reasons.append({"rule": "EMPTY_BATCH", "detail": "批次内没有奶具"})
        return reasons

    sets: dict[str, list] = {}
    for it in items:
        if it["set_id"]:
            sets.setdefault(it["set_id"], []).append(it)

    for it in items:
        latest = _latest_payloads(conn, it["item_code"])
        item_reasons = []

        dc = latest.get("disassembly_check")
        if dc is None:
            item_reasons.append(("PARTS_CHECK_MISSING", "缺少拆分检查记录"))
        elif not dc.get("parts_complete"):
            missing = "、".join(dc.get("missing_parts", [])) or "未说明"
            item_reasons.append(("PARTS_MISSING", f"拆分检查发现部件缺失：{missing}"))

        if it["kind"] == "bottle" and it["set_id"]:
            kinds = {x["kind"] for x in sets.get(it["set_id"], [])}
            lack = {"nipple", "cap"} - kinds
            if lack:
                item_reasons.append(
                    ("PARTS_MISSING",
                     f"套装 {it['set_id']} 在本批次中缺少 {sorted(lack)}"))
        cc = latest.get("clean_check")
        if cc is None:
            item_reasons.append(("CLEAN_CHECK_MISSING", "缺少清洁检查记录"))
        elif cc.get("result") != "pass":
            item_reasons.append(("CLEAN_CHECK_FAILED", "清洁检查未通过"))

        if "dried" not in latest:
            item_reasons.append(("NOT_DRIED", "未完成干燥"))
        se = latest.get("sealed")
        if se is None:
            item_reasons.append(("SEAL_MISSING", "未封存"))
        elif not se.get("seal_intact"):
            item_reasons.append(("SEAL_DAMAGED", "封存破损"))

        if for_release and not item_reasons and it["status"] != "sealed":
            item_reasons.append(("ITEM_STATE_NOT_READY", f"当前状态 {it['status']}"))

        for rule, detail in item_reasons:
            reasons.append({"rule": rule, "item_code": it["item_code"], "detail": detail})
    return reasons


def upload_cycle(conn, batch_code: str, points: list, started_at=None, ended_at=None):
    """上传设备周期曲线并绑定到批次。

    迟到数据（批次已有放行/隔离决定后上传）会触发重新判定：
    - 已放行且新评估不达标 -> 自动判定周期失效并沿发放记录传播；
    - 已隔离 -> 记录复核决定，但隔离保持，绝不抹掉。
    """
    batch = get_batch(conn, batch_code)
    if batch["status"] == "invalidated":
        raise ServiceError(409, "BATCH_INVALIDATED", "批次已判定失效，不再接受曲线数据")
    if not points:
        raise ServiceError(400, "BAD_PAYLOAD", "曲线数据点不能为空")
    for p in points:
        if not isinstance(p.get("t"), (int, float)) or not isinstance(p.get("temp"), (int, float)):
            raise ServiceError(400, "BAD_PAYLOAD", "每个数据点需要数值字段 t 与 temp")

    assessment = assess_cycle(points)
    conn.execute(
        "INSERT INTO cycle_uploads(batch_id, points, started_at, ended_at, assessment, uploaded_at) "
        "VALUES (?,?,?,?,?,?)",
        (batch["id"], _j(points), started_at, ended_at, _j(assessment), now_iso()))

    rejudged = None
    if batch["status"] == "open":
        conn.execute("UPDATE batches SET status='cycle_recorded', updated_at=? WHERE id=?",
                     (now_iso(), batch["id"]))
        for it in _batch_items(conn, batch["id"]):
            if it["status"] == "loaded":
                conn.execute("UPDATE items SET status='disinfected' WHERE item_code=?",
                             (it["item_code"],))
                _sys_event(conn, it["item_code"], "disinfected", batch_code,
                           {"batch_code": batch_code})
    elif batch["status"] == "released":
        reasons = evaluate_batch(conn, batch["id"], for_release=False)
        if reasons:
            _do_invalidate(conn, batch,
                           "迟到的设备数据显示周期参数不达标", trigger="late_cycle_data")
            rejudged = "invalidated"
        else:
            _record_decision(conn, batch["id"], "released", [], None,
                             "late_cycle_data", note="迟到数据复核：周期仍达标，放行维持")
            rejudged = "reaffirmed"
    elif batch["status"] == "quarantined":
        reasons = evaluate_batch(conn, batch["id"], for_release=False)
        _record_decision(conn, batch["id"], "quarantined", reasons, None,
                         "late_cycle_data",
                         note="迟到数据已复核，但已采取的隔离不予抹除")
        rejudged = "quarantine_kept"

    fresh = get_batch(conn, batch_code)
    return {"batch_code": batch_code, "assessment": assessment,
            "batch_status": fresh["status"], "rejudged": rejudged}


# ---------------------------------------------------------------- 放行 / 隔离 / 失效

def _quarantine_batch(conn, batch, reasons):
    now = now_iso()
    conn.execute("UPDATE batches SET status='quarantined', updated_at=? WHERE id=?",
                 (now, batch["id"]))
    for it in _batch_items(conn, batch["id"]):
        if it["status"] not in ("distributed", "recalled"):
            conn.execute(
                "UPDATE items SET status='quarantined', current_batch_id=NULL WHERE item_code=?",
                (it["item_code"],))
            _sys_event(conn, it["item_code"], "quarantined", batch["batch_code"],
                       {"batch_code": batch["batch_code"],
                        "reasons": [r["rule"] for r in reasons]})


def release_batch(conn, batch_code: str, approver: str):
    batch = get_batch(conn, batch_code)
    if batch["status"] == "quarantined":
        raise ServiceError(409, "BATCH_QUARANTINED",
                           "批次已被隔离，隔离不可抹除；奶具需重新回收处理")
    if batch["status"] == "released":
        raise ServiceError(409, "ALREADY_RELEASED", "批次已放行")
    if batch["status"] == "invalidated":
        raise ServiceError(409, "BATCH_INVALIDATED", "批次已判定失效")

    reasons = evaluate_batch(conn, batch["id"], for_release=True)
    if reasons:
        _record_decision(conn, batch["id"], "quarantined", reasons, approver, "release_attempt")
        _quarantine_batch(conn, batch, reasons)
        raise ServiceError(409, "RELEASE_BLOCKED", "存在阻断原因，不得放行，批次已隔离",
                           details={"reasons": reasons, "batch_status": "quarantined"},
                           commit=True)

    _record_decision(conn, batch["id"], "released", [], approver, "release")
    conn.execute("UPDATE batches SET status='released', updated_at=? WHERE id=?",
                 (now_iso(), batch["id"]))
    for it in _batch_items(conn, batch["id"]):
        conn.execute("UPDATE items SET status='released' WHERE item_code=?", (it["item_code"],))
        _sys_event(conn, it["item_code"], "released", batch_code,
                   {"batch_code": batch_code, "approver": approver})
    return batch_view(conn, batch_code)


def _do_invalidate(conn, batch, reason: str, trigger: str, approver=None):
    """周期失效传播：在房间的生成回收事项并回收，已使用的生成后续核查事项。"""
    now = now_iso()
    batch_code = batch["batch_code"]
    conn.execute("UPDATE batches SET status='invalidated', updated_at=? WHERE id=?",
                 (now, batch["id"]))
    _record_decision(conn, batch["id"], "invalidated",
                     [{"rule": "CYCLE_INVALID", "detail": reason}], approver, trigger)

    dists = conn.execute("SELECT * FROM distributions WHERE batch_id=?",
                         (batch["id"],)).fetchall()
    for d in dists:
        if d["status"] == "in_room":
            conn.execute("UPDATE distributions SET status='recalled', recalled_at=? WHERE id=?",
                         (now, d["id"]))
            conn.execute("UPDATE items SET status='recalled' WHERE item_code=?",
                         (d["item_code"],))
            _sys_event(conn, d["item_code"], "recalled", batch_code,
                       {"room": d["room"], "reason": reason})
            conn.execute(
                "INSERT OR IGNORE INTO tasks(kind, batch_id, item_code, room, detail, status, created_at) "
                "VALUES ('recall',?,?,?,?, 'open', ?)",
                (batch["id"], d["item_code"], d["room"],
                 f"批次 {batch_code} 周期失效，需从房间 {d['room']} 回收奶具 {d['item_code']}",
                 now))
        elif d["status"] == "used":
            conn.execute(
                "INSERT OR IGNORE INTO tasks(kind, batch_id, item_code, room, detail, status, created_at) "
                "VALUES ('followup_check',?,?,?,?, 'open', ?)",
                (batch["id"], d["item_code"], d["room"],
                 f"批次 {batch_code} 周期失效，奶具 {d['item_code']} 已在房间 {d['room']} 使用，"
                 "需后续核查婴儿健康状况与补消毒", now))

    # 未发放的库存奶具一并隔离
    for it in _batch_items(conn, batch["id"]):
        if it["status"] in ("loaded", "disinfected", "dried", "sealed", "released"):
            conn.execute(
                "UPDATE items SET status='quarantined', current_batch_id=NULL WHERE item_code=?",
                (it["item_code"],))
            _sys_event(conn, it["item_code"], "quarantined", batch_code,
                       {"batch_code": batch_code, "reason": reason})


def invalidate_batch(conn, batch_code: str, reason: str, approver: str | None):
    batch = get_batch(conn, batch_code)
    if batch["status"] == "invalidated":
        raise ServiceError(409, "ALREADY_INVALIDATED", "批次已判定失效")
    if batch["status"] != "released":
        raise ServiceError(409, "NOT_RELEASED",
                           f"批次状态 {batch['status']}，只有已放行批次需要失效传播")
    _do_invalidate(conn, batch, reason, trigger="manual", approver=approver)
    return batch_view(conn, batch_code)


# ---------------------------------------------------------------- 发放与使用

def distribute(conn, batch_code: str, room: str, item_codes: list | None):
    batch = get_batch(conn, batch_code)
    if batch["status"] != "released":
        raise ServiceError(409, "BATCH_NOT_RELEASED",
                           f"批次状态 {batch['status']}，只有已放行批次可以发放")
    items = {it["item_code"]: it for it in _batch_items(conn, batch["id"])}
    targets = item_codes or [c for c, it in items.items() if it["status"] == "released"]
    if not targets:
        raise ServiceError(409, "NOTHING_TO_DISTRIBUTE", "没有可发放的奶具")

    for code in targets:
        it = items.get(code)
        if it is None:
            raise ServiceError(409, "ITEM_NOT_IN_BATCH", f"奶具 {code} 不在批次 {batch_code} 中")
        existing = conn.execute(
            "SELECT * FROM distributions WHERE batch_id=? AND item_code=?",
            (batch["id"], code)).fetchone()
        if existing is not None:
            if existing["room"] != room:
                raise ServiceError(409, "ALREADY_DISTRIBUTED",
                                   f"奶具 {code} 已发放到房间 {existing['room']}")
            continue  # 幂等重发
        if it["status"] != "released":
            raise ServiceError(409, "ITEM_NOT_RELEASED",
                               f"奶具 {code} 状态 {it['status']}，不能发放")
        conn.execute(
            "INSERT INTO distributions(batch_id, item_code, room, status, distributed_at) "
            "VALUES (?,?,?,'in_room',?)",
            (batch["id"], code, room, now_iso()))
        conn.execute("UPDATE items SET status='distributed', current_batch_id=NULL "
                     "WHERE item_code=?", (code,))
        _sys_event(conn, code, "distributed", batch_code,
                   {"batch_code": batch_code, "room": room})
    return distributions_of(conn, batch["id"])


def distributions_of(conn, batch_id: int):
    rows = conn.execute(
        "SELECT * FROM distributions WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_used(conn, distribution_id: int):
    d = conn.execute("SELECT * FROM distributions WHERE id=?", (distribution_id,)).fetchone()
    if d is None:
        raise ServiceError(404, "DISTRIBUTION_NOT_FOUND", f"发放记录 {distribution_id} 不存在")
    if d["status"] == "used":
        return _row_to_dict(d)  # 幂等
    if d["status"] == "recalled":
        raise ServiceError(409, "ALREADY_RECALLED", "该发放记录已回收，不能标记使用")
    conn.execute("UPDATE distributions SET status='used', used_at=? WHERE id=?",
                 (now_iso(), distribution_id))
    _sys_event(conn, d["item_code"], "used", f"dist-{distribution_id}",
               {"room": d["room"]})
    return _row_to_dict(conn.execute("SELECT * FROM distributions WHERE id=?",
                                     (distribution_id,)).fetchone())


# ---------------------------------------------------------------- 视图

def batch_view(conn, batch_code: str) -> dict:
    batch = get_batch(conn, batch_code)
    items = [_row_to_dict(r) for r in _batch_items(conn, batch["id"])]

    uploads = conn.execute(
        "SELECT * FROM cycle_uploads WHERE batch_id=? ORDER BY id", (batch["id"],)).fetchall()
    cycle = None
    if uploads:
        u = uploads[-1]
        cycle = {
            "points": json.loads(u["points"]),
            "assessment": json.loads(u["assessment"]),
            "started_at": u["started_at"],
            "ended_at": u["ended_at"],
            "uploaded_at": u["uploaded_at"],
            "upload_count": len(uploads),
        }

    decisions = conn.execute(
        "SELECT * FROM decisions WHERE batch_id=? ORDER BY id", (batch["id"],)).fetchall()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE batch_id=? ORDER BY id", (batch["id"],)).fetchall()

    view = _row_to_dict(batch)
    view.update({
        "items": items,
        "cycle": cycle,
        "decisions": [
            {**_row_to_dict(d), "reasons": json.loads(d["reasons"])} for d in decisions
        ],
        "distributions": distributions_of(conn, batch["id"]),
        "tasks": [_row_to_dict(t) for t in tasks],
    })
    return view


def item_view(conn, item_code: str) -> dict:
    item = _row_to_dict(get_item(conn, item_code))
    rows = conn.execute(
        "SELECT * FROM item_events WHERE item_code=? ORDER BY id", (item_code,)).fetchall()
    events = []
    for r in rows:
        e = _row_to_dict(r)
        e["payload"] = json.loads(e["payload"])
        events.append(e)
    item["events"] = events
    return item


def list_tasks(conn, status: str | None = None, kind: str | None = None):
    sql, args = "SELECT * FROM tasks", []
    conds = []
    if status:
        conds.append("status=?")
        args.append(status)
    if kind:
        conds.append("kind=?")
        args.append(kind)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [_row_to_dict(r) for r in rows]


def complete_task(conn, task_id: int):
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        raise ServiceError(404, "TASK_NOT_FOUND", f"事项 {task_id} 不存在")
    if t["status"] == "done":
        return _row_to_dict(t)  # 幂等
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                 (now_iso(), task_id))
    return _row_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
