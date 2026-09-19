import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------ 辅助函数

def register(client, code, kind, set_id=None):
    r = client.post("/items", json={"item_code": code, "kind": kind, "set_id": set_id})
    assert r.status_code == 201, r.text
    return r


def event(client, code, event_type, payload=None, key=None, expect=200):
    r = client.post(f"/items/{code}/events", json={
        "event_type": event_type,
        "payload": payload or {},
        "idempotency_key": key or f"test-{uuid.uuid4()}",
    })
    assert r.status_code == expect, r.text
    return r


def to_cleaned(client, code):
    """回收 -> 拆分检查(齐) -> 预洗 -> 清洁检查(通过)。"""
    event(client, code, "collected")
    event(client, code, "disassembly_check", {"parts_complete": True})
    event(client, code, "prewash")
    event(client, code, "clean_check", {"result": "pass"})


def to_sealed(client, code):
    event(client, code, "dried")
    event(client, code, "sealed", {"seal_intact": True})


def make_set(client, set_id, with_cap=True):
    """注册一套奶瓶+奶嘴(+盖件)并处理到 cleaned。"""
    register(client, f"BT-{set_id}", "bottle", set_id)
    register(client, f"NP-{set_id}", "nipple", set_id)
    codes = [f"BT-{set_id}", f"NP-{set_id}"]
    if with_cap:
        register(client, f"CP-{set_id}", "cap", set_id)
        codes.append(f"CP-{set_id}")
    for c in codes:
        to_cleaned(client, c)
    return codes


def make_batch(client, code="B-1", device="DEV-01"):
    r = client.post("/batches", json={
        "batch_code": code, "device_id": device, "operator": "夜班-李"})
    assert r.status_code == 201, r.text
    return r


def load(client, batch, code, expect=200):
    r = client.post(f"/batches/{batch}/load", json={"item_code": code})
    assert r.status_code == expect, r.text
    return r


def good_cycle():
    """0s 25°C -> 60s 92°C -> 900s 93°C -> 960s 40°C：保温约 840s，达标。"""
    return [{"t": 0, "temp": 25}, {"t": 60, "temp": 92},
            {"t": 900, "temp": 93}, {"t": 960, "temp": 40}]


def bad_cycle():
    """前 600s 仅 88°C，之后 95°C 仅 300s：连续保温不足 600s，不达标。"""
    return [{"t": 0, "temp": 88}, {"t": 300, "temp": 88},
            {"t": 600, "temp": 95}, {"t": 900, "temp": 95}]


def upload_cycle(client, batch, points, expect=200):
    r = client.post(f"/batches/{batch}/cycle", json={"points": points})
    assert r.status_code == expect, r.text
    return r


def release(client, batch, approver="王主管", expect=200):
    r = client.post(f"/batches/{batch}/release", json={"approver": approver})
    assert r.status_code == expect, r.text
    return r


def release_blocked(client, batch, approver="王主管"):
    """放行被阻断 -> 409，返回原因列表。"""
    r = release(client, batch, approver, expect=409)
    assert r.json()["error"]["code"] == "RELEASE_BLOCKED"
    return r.json()["error"]["details"]["reasons"]


def build_released_batch(client, batch="B-1", set_id="S1", extra_rack=False):
    """完整跑通到已放行，返回批次内奶具编码列表。"""
    codes = make_set(client, set_id)
    if extra_rack:
        register(client, f"RK-{set_id}", "rack")
        to_cleaned(client, f"RK-{set_id}")
        codes.append(f"RK-{set_id}")
    make_batch(client, batch)
    for c in codes:
        load(client, batch, c)
    upload_cycle(client, batch, good_cycle())
    for c in codes:
        to_sealed(client, c)
    release(client, batch)
    return codes


def rules(reasons):
    return {r["rule"] for r in reasons}
