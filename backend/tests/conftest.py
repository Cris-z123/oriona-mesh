"""pytest 共享夹具。

测试发现规则见 ``backend/pyproject.toml`` 的 ``[tool.pytest.ini_options]``：
``tests/`` 下按 ``unit/``、``integration/``、``contract/``、``architecture/``、``security/``
分目录组织，通过已注册标记区分；``pythonpath=[\".\"]`` 使 ``app`` 包可直接导入。
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """真实应用（含 trace 中间件与统一错误信封处理器）的同步测试客户端。"""
    with TestClient(app) as test_client:
        yield test_client
