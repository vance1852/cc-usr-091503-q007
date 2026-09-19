"""奶具清洗消毒批次放行服务 —— FastAPI 入口。

运行：uvicorn app.main:app --reload
数据库路径由环境变量 MILK_DB 指定，默认 ./milk.db。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import service
from .db import Database
from .service import ServiceError


# ------------------------------------------------------------ 请求模型

class ItemIn(BaseModel):
    item_code: str
    kind: str               # bottle | nipple | cap | rack
    set_id: str | None = None


class EventIn(BaseModel):
    event_type: str         # collected/disassembly_check/prewash/clean_check/dried/sealed
    payload: dict = {}
    idempotency_key: str


class BatchIn(BaseModel):
    batch_code: str
    device_id: str
    operator: str | None = None


class LoadIn(BaseModel):
    item_code: str


class CurvePoint(BaseModel):
    t: float                # 周期内秒偏移
    temp: float             # 温度 °C


class CycleIn(BaseModel):
    points: list[CurvePoint]
    started_at: str | None = None
    ended_at: str | None = None


class ReleaseIn(BaseModel):
    approver: str


class InvalidateIn(BaseModel):
    reason: str
    approver: str | None = None


class DistributeIn(BaseModel):
    room: str
    item_codes: list[str] | None = None   # 缺省发放批次内全部可发放奶具


# ------------------------------------------------------------ 应用工厂

def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="奶具清洗消毒批次放行服务")
    app.state.db = Database(db_path)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message,
                               "details": exc.details}},
        )

    # ---------------- 奶具与扫码事件 ----------------

    @app.post("/items", status_code=201)
    def register_item(body: ItemIn):
        with app.state.db.tx() as conn:
            return service.register_item(conn, body.item_code, body.kind, body.set_id)

    @app.get("/items/{item_code}")
    def get_item(item_code: str):
        with app.state.db.read() as conn:
            return service.item_view(conn, item_code)

    @app.post("/items/{item_code}/events")
    def post_event(item_code: str, body: EventIn):
        with app.state.db.tx() as conn:
            event, deduped = service.record_event(
                conn, item_code, body.event_type, body.payload, body.idempotency_key)
        return {"event": event, "deduplicated": deduped}

    # ---------------- 批次 ----------------

    @app.post("/batches", status_code=201)
    def create_batch(body: BatchIn):
        with app.state.db.tx() as conn:
            return service.create_batch(conn, body.batch_code, body.device_id, body.operator)

    @app.get("/batches")
    def list_batches():
        with app.state.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    @app.get("/batches/{batch_code}")
    def get_batch(batch_code: str):
        """主管视图：装载明细、关键曲线、未通过原因、审批人、受影响去向。"""
        with app.state.db.read() as conn:
            return service.batch_view(conn, batch_code)

    @app.post("/batches/{batch_code}/load")
    def load_item(batch_code: str, body: LoadIn):
        with app.state.db.tx() as conn:
            return service.load_item(conn, batch_code, body.item_code)

    @app.post("/batches/{batch_code}/cycle")
    def upload_cycle(batch_code: str, body: CycleIn):
        with app.state.db.tx() as conn:
            return service.upload_cycle(
                conn, batch_code, [p.model_dump() for p in body.points],
                body.started_at, body.ended_at)

    @app.post("/batches/{batch_code}/release")
    def release_batch(batch_code: str, body: ReleaseIn):
        with app.state.db.tx() as conn:
            return service.release_batch(conn, batch_code, body.approver)

    @app.post("/batches/{batch_code}/invalidate")
    def invalidate_batch(batch_code: str, body: InvalidateIn):
        with app.state.db.tx() as conn:
            return service.invalidate_batch(conn, batch_code, body.reason, body.approver)

    @app.post("/batches/{batch_code}/distribute")
    def distribute(batch_code: str, body: DistributeIn):
        with app.state.db.tx() as conn:
            return service.distribute(conn, batch_code, body.room, body.item_codes)

    # ---------------- 发放与事项 ----------------

    @app.post("/distributions/{distribution_id}/use")
    def mark_used(distribution_id: int):
        with app.state.db.tx() as conn:
            return service.mark_used(conn, distribution_id)

    @app.get("/tasks")
    def list_tasks(status: str | None = None, kind: str | None = None):
        with app.state.db.read() as conn:
            return service.list_tasks(conn, status=status, kind=kind)

    @app.post("/tasks/{task_id}/complete")
    def complete_task(task_id: int):
        with app.state.db.tx() as conn:
            return service.complete_task(conn, task_id)

    return app


app = create_app(os.environ.get("MILK_DB", "milk.db"))
