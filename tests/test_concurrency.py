"""并发装载与迟到设备数据验证（多连接 / 多线程，基于文件 SQLite）。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import service as svc
from app.db import connect, init_db


@pytest.fixture()
def file_conn(tmp_path) -> Path:
    db = tmp_path / "conc.db"
    c = connect(db)
    init_db(c)
    c.close()
    return db


def _prep(db: Path, code: str, item_type: str) -> None:
    c = connect(db)
    try:
        svc.create_item(c, code, item_type)
        svc.collect(c, code, f"collect:{code}")
        svc.split_check(c, code, True, True, f"check:{code}")
        svc.prewash(c, code, f"prewash:{code}")
    finally:
        c.close()


def test_concurrent_load_same_item_different_batches(file_conn):
    """两个线程同时把同一奶具装进不同批次：有且仅有一个成功。"""
    db = file_conn
    _prep(db, "R-1", "rack")
    _prep(db, "R-2", "rack")
    _prep(db, "B-1", "bottle")
    for c_ in (connect(db),):
        c_.execute("INSERT INTO batches (id, device_id, rack_code) "
                   "VALUES ('LOT-1','D','R-1'),('LOT-2','D','R-2')")
        c_.commit()
        c_.close()

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(batch: str) -> None:
        conn = connect(db)
        try:
            barrier.wait()
            try:
                svc.add_member(conn, batch, "B-1")
                results[batch] = "ok"
            except svc.ServiceError as e:
                results[batch] = f"conflict:{e.code}"
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=("LOT-1",))
    t2 = threading.Thread(target=worker, args=("LOT-2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == ["conflict:conflict", "ok"]
    # 库里只有一条有效占用
    checker = connect(db)
    rows = checker.execute(
        "SELECT batch_id FROM batch_members WHERE item_code='B-1' AND active=1"
    ).fetchall()
    assert len(rows) == 1
    checker.close()


def test_concurrent_load_same_item_same_batch(file_conn):
    """同一批次并发扫码同一件：只产生一条成员记录，两边都不报 500。"""
    db = file_conn
    _prep(db, "R-1", "rack")
    _prep(db, "B-1", "bottle")
    c = connect(db)
    c.execute("INSERT INTO batches (id, device_id, rack_code) VALUES ('LOT-1','D','R-1')")
    c.commit(); c.close()

    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        conn = connect(db)
        try:
            barrier.wait()
            added = svc.add_member(conn, "LOT-1", "B-1")["added"]
            with lock:
                outcomes.append(added)
        finally:
            conn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes) == [False, True]
    checker = connect(db)
    n = checker.execute(
        "SELECT COUNT(*) c FROM batch_members "
        "WHERE item_code='B-1' AND batch_id='LOT-1'"
    ).fetchone()["c"]
    assert n == 1
    checker.close()


def test_concurrent_rescan_single_event(file_conn):
    """并发重传同一扫码事件：事件只推进一次。"""
    db = file_conn
    _prep(db, "B-9", "bottle")  # 已回收
    barrier = threading.Barrier(2)
    advanced: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = connect(db)
        try:
            barrier.wait()
            r = svc.collect(conn, "B-9", "same-scan-key")
            with lock:
                advanced.append(r["advanced"])
        finally:
            conn.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(advanced) == [False, True]


def test_concurrent_curve_and_release_real_late_arrival(file_conn):
    """还原晨间场景：批次已走到待放行，迟到十分钟的保温数据才上传。

    曲线若不达标则之后任何放行尝试都被拦截；先放行后收到不达标曲线则判失效。
    """
    db = file_conn
    for code, t in [("R-1", "rack"), ("B-1", "bottle")]:
        _prep(db, code, t)
    c = connect(db)
    svc.create_batch(c, "LOT-1", "DEV-1", "R-1")
    svc.add_member(c, "LOT-1", "B-1")
    svc.complete_loading(c, "LOT-1")
    c.close()

    # 曲线迟到 10 分钟以上且保温温度不足
    late = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(
        timespec="seconds")
    c = connect(db)
    out = svc.submit_curve(c, "LOT-1", "msg-late", "DEV-1",
                           [(0, 90.0), (300, 90.0)], drying_seconds=300,
                           event_time=late)
    assert out["late"] is True
    svc.mark_dried(c, "LOT-1")
    svc.seal_batch(c, "LOT-1")
    with pytest.raises(svc.ServiceError):
        svc.release(c, "LOT-1", "主管甲")
    c.close()
