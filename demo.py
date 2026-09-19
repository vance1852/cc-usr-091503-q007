"""晨间场景端到端演示脚本。

运行：.venv/bin/python demo.py
覆盖：回收→检查→预洗→装载（混入未预洗件）→撤出→迟到保温曲线→
干燥→封存（含破损）→放行拦截→返工→放行→使用→事后周期失效→回收/核查事项。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import service as svc
from app.db import connect, init_db


def main() -> None:
    conn = connect(":memory:")
    init_db(conn)

    # 1. 奶具登记与前段工序
    for code, t in [("R-1", "rack"), ("B-1", "bottle"), ("B-2", "bottle"),
                    ("N-1", "nipple"), ("N-2", "nipple"), ("C-1", "cap")]:
        svc.create_item(conn, code, t)
    for code in ["R-1", "B-1", "B-2", "N-1", "C-1"]:
        svc.collect(conn, code, f"scan-collect-{code}")
        svc.split_check(conn, code, True, True, f"scan-check-{code}")
        svc.prewash(conn, code, f"scan-pw-{code}")
    # N-2 只回收、检查，漏预洗，稍后混入架子
    svc.collect(conn, "N-2", "scan-collect-N-2")
    svc.split_check(conn, "N-2", True, True, "scan-check-N-2")

    # 2. 装载：N-2 未经预洗被混入
    svc.create_batch(conn, "LOT-0919-01", "STER-A2", "R-1")
    for code in ["B-1", "B-2", "N-1", "C-1", "N-2"]:
        svc.add_member(conn, "LOT-0919-01", code)
    # 主管晨间抽查发现混入件，撤出并隔离
    print("撤出:", svc.remove_member(
        conn, "LOT-0919-01", "N-2", "混入未经预洗配件"))
    svc.complete_loading(conn, "LOT-0919-01")

    # 3. 保温阶段记录迟到十分钟才上传
    late = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(
        timespec="seconds")
    curve = svc.submit_curve(
        conn, "LOT-0919-01", "dev-msg-7788", "STER-A2",
        [(0, 94.8), (60, 95.6), (180, 95.1), (300, 94.9)],
        drying_seconds=420, event_time=late)
    print("曲线迟到:", curve["late"], "温度:", curve["min_temp_c"],
          "时长:", curve["hold_seconds"])

    # 4. 干燥、封存；C-1 封存破损
    svc.mark_dried(conn, "LOT-0919-01")
    svc.seal_batch(conn, "LOT-0919-01", intact={"C-1": False})
    try:
        svc.release(conn, "LOT-0919-01", "主管-王")
    except svc.ServiceError as e:
        print("首次放行被拒:", e.detail)

    # 5. C-1 撤出返工、重新封存后放行
    svc.remove_member(conn, "LOT-0919-01", "C-1", "封存破损返工")
    svc.dequarantine(conn, "C-1", "返工-李", "dq-c1")
    svc.seal_batch(conn, "LOT-0919-01")
    print("放行:", svc.release(conn, "LOT-0919-01", "主管-王", "302新生儿病房"))

    # 6. B-1 已在病房使用，B-2 仍在房间
    svc.mark_used(conn, "B-1", "use-b1")

    # 7. 事后发现该周期保温探头校准失效 -> 批次失效传播
    out = svc.invalidate(conn, "LOT-0919-01", "保温探头校准过期，周期参数无效")
    print("失效事项:")
    for f in out["followups"]:
        print("  -", f["item"], f["kind"], f["detail"])

    print("\n主管视图:")
    print(json.dumps(svc.batch_detail(conn, "LOT-0919-01"),
                     ensure_ascii=False, indent=2, default=str))
    conn.close()


if __name__ == "__main__":
    main()
