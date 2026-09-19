"""主流程与放行阻断规则。"""

from conftest import (bad_cycle, build_released_batch, event, good_cycle, load,
                      make_batch, make_set, register, release, release_blocked,
                      rules, to_cleaned, to_sealed, upload_cycle)


def test_happy_path_release_and_supervisor_view(client):
    codes = build_released_batch(client, batch="B-1", set_id="S1", extra_rack=True)

    r = client.post("/batches/B-1/distribute", json={"room": "101"})
    assert r.status_code == 200, r.text
    assert len(r.json()) == len(codes)

    view = client.get("/batches/B-1").json()
    assert view["status"] == "released"
    # 装载明细
    assert {i["item_code"] for i in view["items"]} == set(codes)
    # 关键曲线与评估
    assert view["cycle"]["assessment"]["passed"] is True
    assert view["cycle"]["assessment"]["longest_hold_s"] >= 600
    assert len(view["cycle"]["points"]) == 4
    # 审批人
    assert view["decisions"][-1]["decision"] == "released"
    assert view["decisions"][-1]["approver"] == "王主管"
    # 去向
    assert {d["room"] for d in view["distributions"]} == {"101"}
    assert all(d["status"] == "in_room" for d in view["distributions"])

    item = client.get(f"/items/{codes[0]}").json()
    kinds = [e["event_type"] for e in item["events"]]
    assert kinds == ["collected", "disassembly_check", "prewash", "clean_check",
                     "loaded", "disinfected", "dried", "sealed", "released",
                     "distributed"]


def test_missing_set_cap_blocks_release(client):
    # 套装 S2 只有奶瓶+奶嘴，盖件缺失
    codes = make_set(client, "S2", with_cap=False)
    make_batch(client, "B-2")
    for c in codes:
        load(client, "B-2", c)
    upload_cycle(client, "B-2", good_cycle())
    for c in codes:
        to_sealed(client, c)

    reasons = release_blocked(client, "B-2")
    assert "PARTS_MISSING" in rules(reasons)
    assert any("S2" in r["detail"] and "cap" in r["detail"] for r in reasons)

    view = client.get("/batches/B-2").json()
    assert view["status"] == "quarantined"
    assert view["decisions"][-1]["decision"] == "quarantined"
    # 奶具被隔离并脱离批次，可重新回收处理
    item = client.get("/items/BT-S2").json()
    assert item["status"] == "quarantined"
    assert item["current_batch_id"] is None


def test_seal_damaged_blocks_release(client):
    codes = make_set(client, "S3")
    make_batch(client, "B-3")
    for c in codes:
        load(client, "B-3", c)
    upload_cycle(client, "B-3", good_cycle())
    for c in codes:
        event(client, c, "dried")
    # 其中一只奶瓶封存破损
    event(client, codes[0], "sealed", {"seal_intact": False})
    for c in codes[1:]:
        event(client, c, "sealed", {"seal_intact": True})

    reasons = release_blocked(client, "B-3")
    assert "SEAL_DAMAGED" in rules(reasons)
    assert any(r.get("item_code") == codes[0] for r in reasons
               if r["rule"] == "SEAL_DAMAGED")
    assert client.get("/batches/B-3").json()["status"] == "quarantined"


def test_cycle_out_of_spec_blocks_release(client):
    codes = make_set(client, "S4")
    make_batch(client, "B-4")
    for c in codes:
        load(client, "B-4", c)
    upload_cycle(client, "B-4", bad_cycle())
    for c in codes:
        to_sealed(client, c)

    reasons = release_blocked(client, "B-4")
    assert "CYCLE_PARAMS_OUT_OF_SPEC" in rules(reasons)
    assert client.get("/batches/B-4").json()["status"] == "quarantined"


def test_release_without_cycle_data_blocks(client):
    codes = make_set(client, "S5")
    make_batch(client, "B-5")
    for c in codes:
        load(client, "B-5", c)  # 注意：未上传周期曲线

    reasons = release_blocked(client, "B-5")
    assert "CYCLE_DATA_MISSING" in rules(reasons)


def test_unprewashed_item_cannot_be_loaded(client):
    """晨间抽查场景：装载架混入未经预洗的配件 -> 装载即被拒绝。"""
    register(client, "RK-9", "rack")
    event(client, "RK-9", "collected")  # 只回收，未预洗
    make_batch(client, "B-6")
    r = load(client, "B-6", "RK-9", expect=409)
    assert r.json()["error"]["code"] == "ITEM_NOT_READY"

    # 清洁检查未通过同样不能装载
    register(client, "BT-9", "bottle", "S9")
    event(client, "BT-9", "collected")
    event(client, "BT-9", "disassembly_check", {"parts_complete": True})
    event(client, "BT-9", "prewash")
    event(client, "BT-9", "clean_check", {"result": "fail"})
    r = load(client, "B-6", "BT-9", expect=409)
    assert r.json()["error"]["code"] == "ITEM_NOT_READY"


def test_event_idempotency_and_no_double_advance(client):
    register(client, "BT-7", "bottle", "S7")
    r1 = event(client, "BT-7", "collected", key="scan-001")
    r2 = event(client, "BT-7", "collected", key="scan-001")  # 扫码重传
    assert r2.json()["deduplicated"] is True
    assert r1.json()["event"]["id"] == r2.json()["event"]["id"]
    assert client.get("/items/BT-7").json()["status"] == "collected"

    # 同一状态推进换 key 重试 -> 409，不重复推进
    r = event(client, "BT-7", "collected", key="scan-002", expect=409)
    assert r.json()["error"]["code"] == "BAD_TRANSITION"

    # 幂等键被别的奶具/事件占用 -> 409
    register(client, "NP-7", "nipple", "S7")
    r = event(client, "NP-7", "collected", key="scan-001", expect=409)
    assert r.json()["error"]["code"] == "KEY_REUSED"

    # 事件流水只有一条 collected
    kinds = [e["event_type"] for e in client.get("/items/BT-7").json()["events"]]
    assert kinds == ["collected"]


def test_double_load_same_batch_idempotent_other_batch_conflict(client):
    register(client, "BT-8", "bottle", "S8")
    to_cleaned(client, "BT-8")
    make_batch(client, "B-8a")
    make_batch(client, "B-8b")

    r = load(client, "B-8a", "BT-8")  # 重复装载同批次 -> 幂等
    assert r.json()["deduplicated"] is False
    r = load(client, "B-8a", "BT-8")
    assert r.json()["deduplicated"] is True

    r = load(client, "B-8b", "BT-8", expect=409)  # 另一批次 -> 冲突
    assert r.json()["error"]["code"] == "ITEM_ALREADY_IN_BATCH"


def test_distribute_requires_released_and_is_idempotent(client):
    codes = make_set(client, "S10")
    make_batch(client, "B-10")
    for c in codes:
        load(client, "B-10", c)
    upload_cycle(client, "B-10", good_cycle())
    for c in codes:
        to_sealed(client, c)

    # 未放行不能发放
    r = client.post("/batches/B-10/distribute", json={"room": "201"})
    assert r.status_code == 409

    release(client, "B-10")
    d1 = client.post("/batches/B-10/distribute",
                     json={"room": "201", "item_codes": codes[:2]})
    assert d1.status_code == 200
    # 重复发放同房间 -> 幂等，不产生重复记录
    d2 = client.post("/batches/B-10/distribute",
                     json={"room": "201", "item_codes": codes[:2]})
    assert d2.status_code == 200
    assert len(d1.json()) == len(d2.json()) == 2
    # 同奶具改房间 -> 冲突
    r = client.post("/batches/B-10/distribute",
                    json={"room": "202", "item_codes": codes[:1]})
    assert r.status_code == 409
