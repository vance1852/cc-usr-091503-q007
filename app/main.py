"""FastAPI 入口：奶具清洗消毒批次放行服务。"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import service as svc
from .db import connect, init_db
from .schemas import (
    CreateBatchIn,
    CreateItemIn,
    CurveIn,
    DequarantineIn,
    IdemIn,
    InvalidateIn,
    QuarantineIn,
    ReleaseIn,
    RemoveMemberIn,
    SealIn,
    SplitCheckIn,
    UsedIn,
)

DB_PATH = os.environ.get("STERILIZER_DB", "sterilizer.db")

app = FastAPI(
    title="奶具清洗消毒批次放行服务",
    version="1.0.0",
    description="回收→拆分检查→预洗→装载→消毒周期→干燥→封存→放行发放，"
    "绑定设备温度时长曲线，支持迟到数据重判与失效后去向核查。",
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(DB_PATH)
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(svc.ServiceError)
async def service_error_handler(_request: Request, exc: svc.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "detail": exc.detail}},
    )


# ---------------------------------------------------------------------------
# 奶具身份与前段工序
# ---------------------------------------------------------------------------
@app.post("/items", status_code=201, tags=["items"])
def create_item(body: CreateItemIn, conn: sqlite3.Connection = Depends(get_conn)):
    row = svc.create_item(conn, body.code, body.item_type)
    return {"code": row["code"], "item_type": row["item_type"]}


@app.get("/items/{code}", tags=["items"])
def get_item(code: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
    if row is None:
        raise svc.ServiceError("no_item", f"奶具不存在: {code}", 404)
    latest = svc.latest_event(conn, code)
    return {
        "code": row["code"], "item_type": row["item_type"],
        "state": latest["event_type"] if latest else None,
        "quarantined": svc.is_quarantined(conn, code),
    }


@app.post("/items/{code}/collect", tags=["items"])
def collect_item(code: str, body: IdemIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    return svc.collect(conn, code, body.idem_key)


@app.post("/items/{code}/split-check", tags=["items"])
def split_check_item(code: str, body: SplitCheckIn,
                     conn: sqlite3.Connection = Depends(get_conn)):
    return svc.split_check(
        conn, code, body.passed, body.parts_complete, body.idem_key, body.note)


@app.post("/items/{code}/prewash", tags=["items"])
def prewash_item(code: str, body: IdemIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    return svc.prewash(conn, code, body.idem_key)


@app.post("/items/{code}/quarantine", tags=["items"])
def quarantine_item(code: str, body: QuarantineIn,
                    conn: sqlite3.Connection = Depends(get_conn)):
    return svc.quarantine(conn, code, body.reason, body.idem_key)


@app.post("/items/{code}/dequarantine", tags=["items"])
def dequarantine_item(code: str, body: DequarantineIn,
                      conn: sqlite3.Connection = Depends(get_conn)):
    return svc.dequarantine(conn, code, body.approver, body.idem_key)


@app.post("/items/{code}/used", tags=["items"])
def mark_used_item(code: str, body: UsedIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    return svc.mark_used(conn, code, body.idem_key)


# ---------------------------------------------------------------------------
# 装载批次
# ---------------------------------------------------------------------------
@app.post("/batches", status_code=201, tags=["batches"])
def create_batch(body: CreateBatchIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    row = svc.create_batch(conn, body.batch_id, body.device_id, body.rack_code)
    return {"batch_id": row["id"], "device_id": row["device_id"],
            "rack": row["rack_code"], "status": row["status"]}


@app.post("/batches/{batch_id}/members/{code}", tags=["batches"])
def add_member(batch_id: str, code: str,
               conn: sqlite3.Connection = Depends(get_conn)):
    return svc.add_member(conn, batch_id, code)


@app.post("/batches/{batch_id}/members/{code}/remove", tags=["batches"])
def remove_member(batch_id: str, code: str, body: RemoveMemberIn,
                  conn: sqlite3.Connection = Depends(get_conn)):
    return svc.remove_member(conn, batch_id, code, body.reason)


@app.post("/batches/{batch_id}/complete-loading", tags=["batches"])
def complete_loading(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return svc.complete_loading(conn, batch_id)


@app.post("/batches/{batch_id}/curve", tags=["batches"])
def submit_curve(batch_id: str, body: CurveIn,
                 conn: sqlite3.Connection = Depends(get_conn)):
    return svc.submit_curve(
        conn, batch_id, body.message_id, body.device_id,
        body.hold_samples, body.drying_seconds, body.event_time)


@app.post("/batches/{batch_id}/dry", tags=["batches"])
def mark_dried(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return svc.mark_dried(conn, batch_id)


@app.post("/batches/{batch_id}/seal", tags=["batches"])
def seal_batch(batch_id: str, body: SealIn,
               conn: sqlite3.Connection = Depends(get_conn)):
    return svc.seal_batch(conn, batch_id, body.intact)


@app.post("/batches/{batch_id}/evaluate", tags=["batches"])
def evaluate(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return svc.evaluate(conn, batch_id)


@app.post("/batches/{batch_id}/release", tags=["batches"])
def release(batch_id: str, body: ReleaseIn,
            conn: sqlite3.Connection = Depends(get_conn)):
    return svc.release(conn, batch_id, body.approver, body.destination)


@app.post("/batches/{batch_id}/invalidate", tags=["batches"])
def invalidate(batch_id: str, body: InvalidateIn,
               conn: sqlite3.Connection = Depends(get_conn)):
    return svc.invalidate(conn, batch_id, body.reason)


@app.get("/batches/{batch_id}", tags=["batches"])
def get_batch(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """主管视图：装载明细、关键曲线、未通过原因、审批人、受影响去向。"""
    return svc.batch_detail(conn, batch_id)


@app.get("/batches/{batch_id}/events", tags=["batches"])
def batch_events(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """批次完整事件流水（审计用）。"""
    svc._require_batch(conn, batch_id)
    rows = conn.execute(
        """SELECT item_code, event_type, event_time, recorded_at, payload
           FROM item_events WHERE batch_id=? ORDER BY id""",
        (batch_id,),
    ).fetchall()
    return {"batch_id": batch_id, "events": [dict(r) for r in rows]}


@app.post("/followups/{followup_id}/resolve", tags=["followups"])
def resolve_followup(followup_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return svc.resolve_followup(conn, followup_id)


@app.get("/followups", tags=["followups"])
def list_followups(status: str | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)):
    sql = "SELECT * FROM followups"
    args: tuple = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY id"
    return {"followups": [dict(r) for r in conn.execute(sql, args)]}
