import pytest

from common.db import Database


@pytest.fixture
def db(tmp_path):
    """所有测试共享的临时数据库（WAL）。"""
    d = Database(tmp_path / "test.db")
    d.ensure_schema()
    return d
