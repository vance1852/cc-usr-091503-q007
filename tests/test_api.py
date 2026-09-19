"""HTTP API 端到端测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _make(client, code, item_type, *, prewash=True, passed=True, parts=True):
    client.post("/items", json={"code": code, "item_type": item_type})
    client.post(f"/items/{code}/collect", json={"idem_key": f"c-{code}"})
    client.post(f"/items/{code}/split-check", json={
        "idem_key": f"k-{code}", "passed": passed, "parts_complete": parts})
    if prewash:
        client.post(f"/items/{code}/prewash", json={"idem_key": f"p-{code}"})


def _full_batch(client, batch="LOT-1", members=("B-1", "N-1"),
                release=False, curve=None, late=False):
    _make(client, "R-1", "rack")
    for code in members:
        _make(client, code, "bottle")
    client.post("/batches", json={"batch_id": batch, "device_id": "D-1",
                                  "rack_code": "R-1"})
    for code in members:
        r = client.post(f"/batches/{batch}/members/{code}")
        assert r.status_code == 200, r.text
    assert client.post(f"/batches/{batch}/complete-loading").status_code == 200
    et = None
    if late:
        et = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(
            timespec="seconds")
    payload = {"message_id": f"m-{batch}", "device_id": "D-1",
               "hold_samples": curve or [(0, 95.0), (300, 95.0)],
               "drying_seconds": 300, "event_time": et}
    assert client.post(f"/batches/{batch}/curve", json=payload).status_code == 200
    client.post(f"/batches/{batch}/dry")
    client.post(f"/batches/{batch}/seal", json={})
    if release:
        r = client.post(f"/batches/{batch}/release",
                        json={"approver": "主管甲", "destination": "302房"})
        assert r.status_code == 200, r.text


def test_full_flow_http(client):
    _full_batch(client, release=True)
    d = client.get("/batches/LOT-1").json()
    assert d["status"] == "released"
    assert d["approval"]["approver"] == "主管甲"
    assert d["curve"]["hold_seconds"] == 300
    assert d["latest_evaluation"]["passed"] is True


def test_duplicate_load_rejected_http(client):
    _full_batch(client, batch="LOT-1")
    _make(client, "R-2", "rack")
    r = client.post("/batches", json={"batch_id": "LOT-2", "device_id": "D-1",
                                      "rack_code": "R-2"})
    assert r.status_code == 201
    r = client.post("/batches/LOT-2/members/B-1")
    assert r.status_code == 409
    assert "LOT-1" in r.json()["error"]["detail"]


def test_release_denied_http(client):
    # 未预洗件混入
    _make(client, "R-1", "rack")
    _make(client, "N-9", "nipple", prewash=False)
    client.post("/batches", json={"batch_id": "LOT-1", "device_id": "D-1",
                                  "rack_code": "R-1"})
    client.post("/batches/LOT-1/members/N-9")
    client.post("/batches/LOT-1/complete-loading")
    client.post("/batches/LOT-1/curve", json={
        "message_id": "m1", "device_id": "D-1",
        "hold_samples": [(0, 95.0), (300, 95.0)], "drying_seconds": 300})
    client.post("/batches/LOT-1/dry")
    client.post("/batches/LOT-1/seal")
    r = client.post("/batches/LOT-1/release", json={"approver": "主管甲"})
    assert r.status_code == 422
    d = client.get("/batches/LOT-1").json()
    assert d["approval"]["decision"] == "denied"
    assert any(x["code"] == "not_prewashed"
               for x in d["latest_evaluation"]["reasons"])


def test_late_curve_after_release_invalidation_http(client):
    _full_batch(client, release=True, members=("B-1", "N-1"))
    # B-1 已在 302 房使用，N-1 仍在房间
    assert client.post("/items/B-1/used", json={"idem_key": "u-1"}).status_code == 200

    late = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(
        timespec="seconds")
    r = client.post("/batches/LOT-1/curve", json={
        "message_id": "m-real", "device_id": "D-1",
        "hold_samples": [(0, 88.0), (300, 88.0)], "drying_seconds": 300,
        "event_time": late})
    assert r.status_code == 200 and r.json()["late"] is True
    d = client.get("/batches/LOT-1").json()
    assert d["status"] == "invalidated"
    assert d["invalidation"]["reason"]
    kinds = {f["item_code"]: f["kind"] for f in d["affected_followups"]}
    assert kinds["B-1"] == "investigation"
    assert kinds["N-1"] == "recall"
    # 装载架未发放，不留去向事项
    assert "R-1" not in kinds

    fu = client.get("/followups?status=open").json()["followups"]
    assert {f["kind"] for f in fu} == {"recall", "investigation"}


def test_rescan_idempotent_http(client):
    _make(client, "B-7", "bottle")
    r1 = client.post("/items/B-7/collect", json={"idem_key": "scan-x"}).json()
    r2 = client.post("/items/B-7/collect", json={"idem_key": "scan-x"}).json()
    assert r1["advanced"] is True and r2["advanced"] is False
    # 设备消息重传
    _full_batch(client, batch="LOT-9", members=("B-8",))
    dup = client.post("/batches/LOT-9/curve", json={
        "message_id": "m-LOT-9", "device_id": "D-1",
        "hold_samples": [(0, 80.0), (300, 80.0)], "drying_seconds": 1})
    assert dup.json()["duplicate"] is True
