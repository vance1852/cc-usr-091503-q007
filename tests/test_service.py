"""端到端业务规则测试（服务层）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import service as svc

GOOD_CURVE = [(0, 95.0), (60, 96.1), (180, 95.4), (300, 95.0)]


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------
def prep_item(conn, code, item_type="bottle", *, passed=True, parts=True,
              do_prewash=None, collect=True):
    svc.create_item(conn, code, item_type)
    if collect:
        svc.collect(conn, code, f"collect:{code}")
        svc.split_check(conn, code, passed=passed, parts_complete=parts,
                        idem_key=f"check:{code}")
        if do_prewash is None:
            do_prewash = passed and parts
        if do_prewash:
            svc.prewash(conn, code, f"prewash:{code}")


def build_ready_batch(conn, batch="LOT-1", codes=("B-1", "N-1", "C-1"),
                      *, curve=GOOD_CURVE, drying=300, release=False,
                      destination="病房"):
    """装配一个走完装载→曲线→干燥→封存（可直接放行）的批次。"""
    prep_item(conn, "R-1", "rack")
    for i, code in enumerate(codes):
        types = {"B": "bottle", "N": "nipple", "C": "cap"}
        prep_item(conn, code, types.get(code[0], "bottle"))
    svc.create_batch(conn, batch, "DEV-1", "R-1")
    for code in codes:
        svc.add_member(conn, batch, code)
    svc.complete_loading(conn, batch)
    if curve is not None:
        svc.submit_curve(conn, batch, f"msg-{batch}", "DEV-1", curve,
                         drying_seconds=drying)
    svc.mark_dried(conn, batch)
    svc.seal_batch(conn, batch)
    if release:
        svc.release(conn, batch, "主管甲", destination)
    return batch


# ---------------------------------------------------------------------------
# 正常放行全流程
# ---------------------------------------------------------------------------
def test_happy_path_release(conn):
    build_ready_batch(conn, release=True)
    detail = svc.batch_detail(conn, "LOT-1")
    assert detail["status"] == "released"
    assert detail["latest_evaluation"]["passed"] is True
    assert detail["approval"]["approver"] == "主管甲"
    assert detail["curve"]["min_temp_c"] == 95.0
    assert detail["curve"]["hold_seconds"] == 300
    assert {m["item"] for m in detail["loading"]["members"]} == {
        "R-1", "B-1", "N-1", "C-1"}
    released = {m["item"]: m["state"]
                for m in detail["loading"]["members"]
                if m["item_type"] != "rack"}
    assert all(s == "released" for s in released.values())
    # 装载架留在消毒间，不随批发放
    rack = next(m for m in detail["loading"]["members"] if m["item_type"] == "rack")
    assert rack["state"] != "released"


# ---------------------------------------------------------------------------
# 各类闸门拦截
# ---------------------------------------------------------------------------
def test_not_prewashed_part_is_blocked(conn):
    """场景还原：未经预洗的配件混入装载架，不得放行。"""
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "N-1", "nipple", do_prewash=False)  # 混入的未预洗奶嘴
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    svc.add_member(conn, "LOT-1", "N-1")
    svc.complete_loading(conn, "LOT-1")
    svc.submit_curve(conn, "LOT-1", "m1", "DEV-1", GOOD_CURVE, 300)
    svc.mark_dried(conn, "LOT-1")
    svc.seal_batch(conn, "LOT-1")

    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert ei.value.status == 422
    detail = svc.batch_detail(conn, "LOT-1")
    codes = {r["code"] for r in detail["latest_evaluation"]["reasons"]}
    assert "not_prewashed" in codes

    # 装载复核撤出未预洗件并隔离，余下可放行
    svc.remove_member(conn, "LOT-1", "N-1", reason="混入未经预洗配件")
    assert svc.is_quarantined(conn, "N-1")
    svc.release(conn, "LOT-1", "主管甲")
    assert svc.batch_detail(conn, "LOT-1")["status"] == "released"


def test_missing_parts_blocks_and_keeps_quarantine(conn):
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "B-1", "bottle", parts=False)
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    # 缺件件已被隔离，装载即被拒
    with pytest.raises(svc.Conflict):
        svc.add_member(conn, "LOT-1", "B-1")


def test_inspection_failed_blocks_until_rework(conn):
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "B-1", "bottle", passed=False)
    assert svc.is_quarantined(conn, "B-1")
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    with pytest.raises(svc.Conflict):
        svc.add_member(conn, "LOT-1", "B-1")
    # 预洗也不允许绕过隔离
    with pytest.raises(svc.Conflict):
        svc.prewash(conn, "B-1", "pw-again")


def test_broken_seal_blocks_release(conn):
    build_ready_batch(conn)
    # 二次封存登记中发现 B-1 封存破损
    svc.seal_batch(conn, "LOT-1", intact={"B-1": False})
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "封存破损" in ei.value.detail
    assert svc.is_quarantined(conn, "B-1")


def test_cycle_params_below_threshold_blocks(conn):
    # 保温温度只有 90℃
    build_ready_batch(conn, curve=[(0, 91.0), (300, 90.0)])
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "保温温度" in ei.value.detail


def test_cycle_hold_too_short_blocks(conn):
    build_ready_batch(conn, curve=[(0, 95.0), (240, 95.0)])
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "保温时长" in ei.value.detail


def test_drying_too_short_blocks(conn):
    build_ready_batch(conn, drying=120)
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "干燥时长" in ei.value.detail


def test_late_curve_arrives_before_release_then_passes(conn):
    """晨间场景：批次封存待放行，保温数据迟到十分钟才上传。

    合格迟到曲线补上后可放行；不合格则继续拦截。两种情况都不改变历史隔离。
    """
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "B-1", "bottle")
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    svc.add_member(conn, "LOT-1", "B-1")
    svc.complete_loading(conn, "LOT-1")
    svc.mark_dried(conn, "LOT-1")  # 干燥先登记，曲线尚未到
    svc.seal_batch(conn, "LOT-1")
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "曲线尚未上传" in ei.value.detail

    late = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(
        timespec="seconds")
    out = svc.submit_curve(conn, "LOT-1", "m-late-ok", "DEV-1", GOOD_CURVE,
                           drying_seconds=300, event_time=late)
    assert out["late"] is True
    svc.release(conn, "LOT-1", "主管甲")  # 重判通过
    assert svc.batch_detail(conn, "LOT-1")["status"] == "released"


def test_curve_missing_blocks_release(conn):
    build_ready_batch(conn, curve=None)
    with pytest.raises(svc.ServiceError) as ei:
        svc.release(conn, "LOT-1", "主管甲")
    assert "曲线尚未上传" in ei.value.detail


# ---------------------------------------------------------------------------
# 迟到设备数据：触发重新判定，但不抹掉已采取的隔离
# ---------------------------------------------------------------------------
def test_late_good_curve_does_not_clear_quarantine(conn):
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "B-1", "bottle")
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    svc.add_member(conn, "LOT-1", "B-1")
    svc.complete_loading(conn, "LOT-1")
    # 设备数据迟到（设备侧时间为 15 分钟前）
    late_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
        timespec="seconds")
    # 先对 B-1 采取了隔离（例如装载复核发现污损）
    svc.quarantine(conn, "B-1", "复核发现残留污渍", "q-1")

    out = svc.submit_curve(conn, "LOT-1", "m-late", "DEV-1", GOOD_CURVE,
                           drying_seconds=300, event_time=late_time)
    assert out["late"] is True
    # 曲线合格，但隔离仍在
    assert svc.is_quarantined(conn, "B-1")
    detail = svc.batch_detail(conn, "LOT-1")
    assert detail["curve"]["late"] is True
    reasons = {r["code"] for r in detail["latest_evaluation"]["reasons"]}
    assert "quarantined" in reasons
    with pytest.raises(svc.ServiceError):
        svc.release(conn, "LOT-1", "主管甲")

    # 返工显式解除隔离后才能继续
    svc.remove_member(conn, "LOT-1", "B-1", reason="返工")
    svc.dequarantine(conn, "B-1", "返工主管", "dq-1")
    assert svc.is_quarantined(conn, "B-1") is False


def test_late_bad_curve_after_release_invalidates(conn):
    build_ready_batch(conn, release=True)
    late_time = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
        timespec="seconds")
    # 放行后迟到的真实曲线不达标 -> 周期无效
    out = svc.submit_curve(conn, "LOT-1", "m-real", "DEV-1",
                           [(0, 89.0), (300, 89.0)], drying_seconds=300,
                           event_time=late_time)
    assert out["late"] is True
    assert svc.batch_detail(conn, "LOT-1")["status"] == "invalidated"


# ---------------------------------------------------------------------------
# 幂等：扫码重传 / 设备消息重传不重复推进
# ---------------------------------------------------------------------------
def test_rescan_does_not_advance_twice(conn):
    svc.create_item(conn, "B-1", "bottle")
    r1 = svc.collect(conn, "B-1", "scan-1")
    r2 = svc.collect(conn, "B-1", "scan-1")  # 同一扫码重传
    assert r1["advanced"] is True and r2["advanced"] is False

    svc.split_check(conn, "B-1", True, True, "scan-2")
    dup = svc.split_check(conn, "B-1", False, True, "scan-2")  # 内容不同也被忽略
    assert dup["advanced"] is False
    # 先到的"通过"结论保持不变，未被重传覆盖
    assert not svc.is_quarantined(conn, "B-1")


def test_device_message_replay_is_idempotent(conn):
    build_ready_batch(conn)
    dup = svc.submit_curve(conn, "LOT-1", "msg-LOT-1", "DEV-1",
                           [(0, 80.0), (300, 80.0)], drying_seconds=1)
    assert dup["duplicate"] is True and dup["accepted"] is False
    # 原合格曲线结论不变
    assert svc.batch_detail(conn, "LOT-1")["curve"]["min_temp_c"] == 95.0


def test_complete_loading_replay_idempotent(conn):
    build_ready_batch(conn)
    again = svc.complete_loading(conn, "LOT-1")
    assert again["advanced"] is False


# ---------------------------------------------------------------------------
# 同一奶具不能同时处于两个装载批次
# ---------------------------------------------------------------------------
def test_item_cannot_be_in_two_batches(conn):
    prep_item(conn, "R-1", "rack")
    prep_item(conn, "R-2", "rack")
    prep_item(conn, "B-1", "bottle")
    svc.create_batch(conn, "LOT-1", "DEV-1", "R-1")
    svc.create_batch(conn, "LOT-2", "DEV-1", "R-2")
    svc.add_member(conn, "LOT-1", "B-1")
    with pytest.raises(svc.Conflict) as ei:
        svc.add_member(conn, "LOT-2", "B-1")
    assert "LOT-1" in ei.value.detail


def test_item_reusable_after_release_new_cycle(conn):
    build_ready_batch(conn, codes=("B-1",), release=True)
    # 发放后周转：回收→检查→预洗，可进入下一批
    svc.collect(conn, "B-1", "collect2")
    svc.split_check(conn, "B-1", True, True, "check2")
    svc.prewash(conn, "B-1", "pw2")
    prep_item(conn, "R-9", "rack")
    svc.create_batch(conn, "LOT-2", "DEV-1", "R-9")
    assert svc.add_member(conn, "LOT-2", "B-1")["added"] is True


# ---------------------------------------------------------------------------
# 失效传播：沿发放记录找出去向
# ---------------------------------------------------------------------------
def test_invalidation_recall_vs_investigation(conn):
    build_ready_batch(conn, codes=("B-1", "B-2", "B-3"), release=True,
                      destination="302病房")
    # B-1 已在房间使用；B-2/B-3 仍在房间
    svc.mark_used(conn, "B-1", "use-1")

    out = svc.invalidate(conn, "LOT-1", "保温探头校准失效，周期无效")
    kinds = {f["item"]: f["kind"] for f in out["followups"]}
    assert kinds["B-1"] == "investigation"
    assert kinds["B-2"] == "recall"
    assert kinds["B-3"] == "recall"

    detail = svc.batch_detail(conn, "LOT-1")
    affected = {f["item_code"]: f["kind"] for f in detail["affected_followups"]}
    assert affected["B-1"] == "investigation"
    # 重复失效不重复生成事项
    again = svc.invalidate(conn, "LOT-1", "再次确认无效")
    assert again["followups"] == []
    detail2 = svc.batch_detail(conn, "LOT-1")
    assert len(detail2["affected_followups"]) == 3
    assert svc.latest_event(conn, "B-2")["event_type"] == "recalled"
    assert svc.latest_event(conn, "B-1")["event_type"] == "used"
    # 失效批次禁止放行
    with pytest.raises(svc.ServiceError):
        svc.release(conn, "LOT-1", "主管甲")
    # 事项可核销
    fid = detail["affected_followups"][0]["id"]
    assert svc.resolve_followup(conn, fid)["status"] == "done"


def test_invalidation_before_release_quarantines_rack(conn):
    build_ready_batch(conn, codes=("B-1",))
    out = svc.invalidate(conn, "LOT-1", "曲线复核不达标")
    assert out["followups"] == []  # 未发放，无去向事项
    assert svc.is_quarantined(conn, "B-1")
    assert svc.is_quarantined(conn, "R-1")


def test_recalled_item_cannot_be_marked_used(conn):
    build_ready_batch(conn, codes=("B-1",), release=True)
    svc.invalidate(conn, "LOT-1", "周期无效")  # B-1 在房间 -> recalled
    with pytest.raises(svc.Conflict):
        svc.mark_used(conn, "B-1", "use-late")


# ---------------------------------------------------------------------------
# 主管视图
# ---------------------------------------------------------------------------
def test_supervisor_view_contents(conn):
    build_ready_batch(conn, release=True, destination="NICU")
    detail = svc.batch_detail(conn, "LOT-1")
    assert detail["loading"]["members"]
    assert detail["curve"]["samples"]["hold_samples"]
    assert detail["curve"]["thresholds"]["hold_seconds"] == 300
    assert detail["approval"]["decision"] == "approved"
    assert detail["affected_followups"] == []
    raw = json.dumps(detail, ensure_ascii=False, default=str)
    assert "NICU" in raw
