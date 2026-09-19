"""并发装载：同一奶具不能同时处于两个装载批次。"""

from concurrent.futures import ThreadPoolExecutor

from conftest import load, make_batch, register, to_cleaned


def test_concurrent_load_same_item_two_batches(client):
    register(client, "BT-C1", "bottle", "SC1")
    to_cleaned(client, "BT-C1")
    make_batch(client, "B-C1")
    make_batch(client, "B-C2")

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(client.post, f"/batches/{b}/load",
                          json={"item_code": "BT-C1"})
                for b in ("B-C1", "B-C2")]
        statuses = sorted(f.result().status_code for f in futs)

    assert statuses == [200, 409]
    item = client.get("/items/BT-C1").json()
    assert item["status"] == "loaded"
    # 全系统只装载进了一个批次
    total = []
    for b in ("B-C1", "B-C2"):
        total += [i["item_code"] for i in client.get(f"/batches/{b}").json()["items"]]
    assert total == ["BT-C1"]


def test_concurrent_load_many_items_no_duplicates(client):
    """20 个奶具各自被两个批次同时抢装：每个奶具最终只处于一个批次。"""
    n = 20
    codes = []
    for i in range(n):
        code = f"BT-M{i}"
        register(client, code, "bottle", f"SM{i}")
        to_cleaned(client, code)
        codes.append(code)
    make_batch(client, "B-M1")
    make_batch(client, "B-M2")

    def attempt(batch, code):
        return client.post(f"/batches/{batch}/load", json={"item_code": code})

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(attempt, b, c) for c in codes for b in ("B-M1", "B-M2")]
        results = [f.result() for f in futs]

    ok = sum(1 for r in results if r.status_code == 200)
    conflict = sum(1 for r in results if r.status_code == 409)
    assert ok == n
    assert conflict == n

    v1 = client.get("/batches/B-M1").json()["items"]
    v2 = client.get("/batches/B-M2").json()["items"]
    in1 = {i["item_code"] for i in v1}
    in2 = {i["item_code"] for i in v2}
    assert in1.isdisjoint(in2)          # 没有奶具同时处于两个批次
    assert in1 | in2 == set(codes)      # 每个奶具恰好被装载一次
