from types import SimpleNamespace

import pytest

import aioresponses.core as _ar_core
from aiohttp.client_reqrep import ClientResponse as _AiohttpClientResponse
from common.db import Database

# --- aioresponses 0.7.9 与 aiohttp>=3.12 的兼容补丁 ---
# aiohttp 3.12 起 ClientResponse.__init__ 新增必填 keyword-only 参数 stream_writer，
# 且 writer=None 时会读 stream_writer.output_size；aioresponses 0.7.9（最新版）未适配。
# 这里替换其内部使用的 ClientResponse 为注入默认 stream_writer 的子类。
# 上游发布兼容版本后可删除本补丁。


class _CompatClientResponse(_AiohttpClientResponse):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)


_ar_core.ClientResponse = _CompatClientResponse
# --- 补丁结束 ---


@pytest.fixture
def db(tmp_path):
    """所有测试共享的临时数据库（WAL）。"""
    d = Database(tmp_path / "test.db")
    d.ensure_schema()
    return d
