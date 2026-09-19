"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ItemType = Literal["bottle", "nipple", "cap", "rack"]


class CreateItemIn(BaseModel):
    code: str = Field(..., examples=["B-001"])
    item_type: ItemType


class IdemIn(BaseModel):
    idem_key: str = Field(..., description="扫码/客户端幂等键，重传复用同一值")


class SplitCheckIn(BaseModel):
    idem_key: str
    passed: bool
    parts_complete: bool
    note: str = ""


class CreateBatchIn(BaseModel):
    batch_id: str = Field(..., examples=["LOT-20260919-01"])
    device_id: str
    rack_code: str


class RemoveMemberIn(BaseModel):
    reason: str = ""


class CurveIn(BaseModel):
    message_id: str = Field(..., description="设备消息唯一 ID，用于重传去重")
    device_id: str
    # [(距周期开始秒数, 温度℃), ...] 保温阶段采样
    hold_samples: list[tuple[float, float]]
    drying_seconds: int
    event_time: str | None = Field(
        None, description="设备侧记录时间 ISO8601；早于当前超过 10 分钟即迟到")


class SealIn(BaseModel):
    intact: dict[str, bool] | None = Field(
        None, description="逐件封存完好性，缺省视为完好")


class ReleaseIn(BaseModel):
    approver: str
    destination: str = "病房"


class UsedIn(BaseModel):
    idem_key: str


class QuarantineIn(BaseModel):
    reason: str
    idem_key: str


class DequarantineIn(BaseModel):
    approver: str
    idem_key: str


class InvalidateIn(BaseModel):
    reason: str
