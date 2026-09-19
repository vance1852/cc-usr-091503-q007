"""迟到设备数据：触发重新判定，但不能抹掉已采取的隔离。"""

from conftest import (bad_cycle, build_released_batch, event, good_cycle, load,
                      make_batch, make_set, release, release_blocked, rules,
                      to_sealed, upload_cycle)


def _distribute_and_use(client, batch, codes):
    """codes[0],codes[1] 发到 101（codes[1] 标记已使用），codes[2] 发到 102。"""
    client.post(f"/batches/{batch}/distribute",
                json={"room": "101", "item_codes": codes[:2]})
    client.post(f"/batches/{batch}/distribute",
                json={"room": "102", "item_codes": codes[2:3]})
    dists = client.get(f"/batches/{batch}").json()["distributions"]
    used = next(d for d in dists if d["item_code"] == codes[1])
    client.post(f"/distributions/{used['id']}/use")
    return dists


def test_late_cycle_data_rejudges_and_propagates(client):
    """放行后迟到的设备数据显示周期无效 -> 自动失效并沿发放记录生成事项。"""
    codes = build_released_batch(client, batch="B-L1", set_id="SL1", extra_rack=True)
    _distribute_and_use(client, "B-L1", codes)

    # 设备数据迟到重传：保温阶段实际不达标
    r = upload_cycle(client, "B-L1", bad_cycle())
    assert r.json()["rejudged"] == "invalidated"
    assert r.json()["batch_status"] == "invalidated"

    view = client.get("/batches/B-L1").json()
    assert view["status"] == "invalidated"
    assert view["decisions"][-1]["decision"] == "invalidated"
    assert view["decisions"][-1]["trigger"] == "late_cycle_data"

    tasks = {(t["kind"], t["item_code"]): t for t in view["tasks"]}
    # 在房间的 -> 回收事项；已使用的 -> 后续核查事项
    assert ("recall", codes[0]) in tasks and tasks[("recall", codes[0])]["room"] == "101"
    assert ("followup_check", codes[1]) in tasks
    assert ("recall", codes[2]) in tasks and tasks[("recall", codes[2])]["room"] == "102"
    # 未发放的装载架被隔离，不产生房间事项
    assert all(t["item_code"] != codes[3] for t in view["tasks"])
    assert client.get(f"/items/{codes[3]}").json()["status"] == "quarantined"

    dists = {d["item_code"]: d["status"] for d in view["distributions"]}
    assert dists[codes[0]] == "recalled"
    assert dists[codes[1]] == "used"      # 已使用保持事实，转入核查事项
    assert dists[codes[2]] == "recalled"

    # 同样的迟到数据再次重传 -> 不重复生成事项
    upload_cycle(client, "B-L1", bad_cycle(), expect=409)  # 已失效批次拒收
    view2 = client.get("/batches/B-L1").json()
    assert len(view2["tasks"]) == len(view["tasks"])


def test_late_data_reaffirms_valid_release(client):
    """迟到数据复核仍达标 -> 放行维持，留痕。"""
    codes = build_released_batch(client, batch="B-L2", set_id="SL2")
    r = upload_cycle(client, "B-L2", good_cycle())
    assert r.json()["rejudged"] == "reaffirmed"
    view = client.get("/batches/B-L2").json()
    assert view["status"] == "released"
    assert view["decisions"][-1]["trigger"] == "late_cycle_data"
    assert view["decisions"][-1]["decision"] == "released"
    assert view["cycle"]["upload_count"] == 2


def test_quarantine_not_erased_by_late_data(client):
    """记录晚上传导致隔离：迟到数据到达后隔离保持，不被抹掉。"""
    codes = make_set(client, "SL3")
    make_batch(client, "B-L3")
    for c in codes:
        load(client, "B-L3", c)
    # 保温记录尚未上传就尝试放行 -> 阻断并隔离
    reasons = release_blocked(client, "B-L3")
    assert "CYCLE_DATA_MISSING" in rules(reasons)
    assert client.get("/batches/B-L3").json()["status"] == "quarantined"

    # 十分钟后设备记录终于上传，曲线其实达标
    r = upload_cycle(client, "B-L3", good_cycle())
    assert r.json()["rejudged"] == "quarantine_kept"

    view = client.get("/batches/B-L3").json()
    assert view["status"] == "quarantined"          # 隔离不被抹掉
    assert view["decisions"][-1]["trigger"] == "late_cycle_data"
    assert view["decisions"][-1]["decision"] == "quarantined"
    assert "不予抹除" in view["decisions"][-1]["note"]

    # 已隔离批次不能再放行
    r = release(client, "B-L3", expect=409)
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"

    # 被隔离的奶具可重新回收，进入新的批次再处理
    for c in codes:
        event(client, c, "collected")
    assert client.get(f"/items/{codes[0]}").json()["status"] == "collected"
