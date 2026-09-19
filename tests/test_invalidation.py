"""批次失效传播与发放后状态。"""

from conftest import build_released_batch


def _invalidate(client, batch, reason="设备校准异常，周期记录不可信", expect=200):
    r = client.post(f"/batches/{batch}/invalidate",
                    json={"reason": reason, "approver": "质控-赵"})
    assert r.status_code == expect, r.text
    return r


def test_manual_invalidate_propagates_to_rooms_and_used(client):
    codes = build_released_batch(client, batch="B-I1", set_id="SI1", extra_rack=True)
    # 101 房：codes[0] 在房间、codes[1] 已使用；102 房：codes[2]；codes[3] 未发放
    client.post("/batches/B-I1/distribute",
                json={"room": "101", "item_codes": codes[:2]})
    client.post("/batches/B-I1/distribute",
                json={"room": "102", "item_codes": codes[2:3]})
    dists = client.get("/batches/B-I1").json()["distributions"]
    used_id = next(d["id"] for d in dists if d["item_code"] == codes[1])
    client.post(f"/distributions/{used_id}/use")

    view = _invalidate(client, "B-I1").json()
    assert view["status"] == "invalidated"
    dec = view["decisions"][-1]
    assert dec["decision"] == "invalidated"
    assert dec["approver"] == "质控-赵"
    assert dec["reasons"][0]["rule"] == "CYCLE_INVALID"

    tasks = {(t["kind"], t["item_code"]): t for t in view["tasks"]}
    assert ("recall", codes[0]) in tasks          # 在房间 -> 回收
    assert ("followup_check", codes[1]) in tasks  # 已使用 -> 后续核查
    assert ("recall", codes[2]) in tasks
    assert len(tasks) == 3                        # 未发放的装载架不产生房间事项

    assert client.get(f"/items/{codes[0]}").json()["status"] == "recalled"
    assert client.get(f"/items/{codes[3]}").json()["status"] == "quarantined"

    # 已回收的发放记录不能再标记使用
    recalled_id = next(d["id"] for d in view["distributions"]
                       if d["item_code"] == codes[0])
    r = client.post(f"/distributions/{recalled_id}/use")
    assert r.status_code == 409

    # 事项可闭环，且完成操作幂等
    task_id = view["tasks"][0]["id"]
    assert client.post(f"/tasks/{task_id}/complete").json()["status"] == "done"
    assert client.post(f"/tasks/{task_id}/complete").json()["status"] == "done"
    open_tasks = client.get("/tasks?status=open").json()
    assert len(open_tasks) == 2

    # 重复失效 -> 409，事项不重复
    _invalidate(client, "B-I1", expect=409)
    assert len(client.get("/tasks").json()) == 3


def test_invalidate_requires_released(client):
    codes = build_released_batch(client, batch="B-I2", set_id="SI2")
    _invalidate(client, "B-I2", "第一次失效").json()
    # 已失效批次不能再次失效
    r = client.post("/batches/B-I2/invalidate", json={"reason": "再次"})
    assert r.status_code == 409
