"""pytest 公共夹具。"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import connect, init_db


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    db_file = tmp_path / "test.db"
    c = connect(db_file)
    init_db(c)
    c.close()

    def get_conn() -> Iterator[sqlite3.Connection]:
        cc = connect(db_file)
        try:
            yield cc
        finally:
            cc.close()

    main.app.dependency_overrides[main.get_conn] = get_conn
    with TestClient(main.app) as cl:
        yield cl
    main.app.dependency_overrides.clear()
