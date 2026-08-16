"""任务真相源架构测试（T088 / plan.md 关键设计决策 5：PostgreSQL 是任务状态唯一真相源）。

- Redis/Celery 仅执行或传输，不承载业务状态：``app/services/``、
  ``app/repositories/``、``app/workers/`` 不得直接使用 Redis 客户端
  （``redis`` 库 / ``redis_lib`` / ``get_redis_client``）；
- Redis 客户端只允许出现在限流基础设施（``app/infrastructure/rate_limit/``
  与限流 HTTP 中间件）、``app/core/redis.py`` 与 ``app/workers/celery_app.py``；
- Celery 不配置结果后端（``result_backend``），任务状态查询全部走 repositories
  的 PostgreSQL 表；worker 在数据库事务内推进任务状态（``FOR UPDATE`` + commit）。

自包含：纯 ``ast`` 源码解析，不依赖数据库/Redis。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 允许直接使用 Redis 客户端的模块（限流基础设施及其 HTTP 适配层、客户端工厂、
# Celery broker 装配）。除此之外任何模块出现 Redis 客户端即视为违规。
_ALLOWED_REDIS_MODULES = {
    "app/core/redis.py",
    "app/workers/celery_app.py",
    "app/api/middleware/rate_limit.py",  # 限流基础设施的 HTTP 适配层（仅限流计数）
    "app/infrastructure/rate_limit/config.py",
    "app/infrastructure/rate_limit/keys.py",
    "app/infrastructure/rate_limit/policies.py",
    "app/infrastructure/rate_limit/redis_limiter.py",
    "app/infrastructure/rate_limit/source_ip.py",
}

# 业务状态层：禁止任何 Redis 客户端使用。
_BUSINESS_LAYERS = ("app/services", "app/repositories", "app/workers")


def _walk_py(rel_dir: str) -> list[str]:
    """返回目录（相对 backend 根）下全部 .py 文件相对路径。"""
    base = BACKEND_ROOT / rel_dir
    if not base.is_dir():
        return []
    return sorted(str(p.relative_to(BACKEND_ROOT)).replace("\\", "/") for p in base.rglob("*.py"))


def _imports(rel_path: str) -> set[str]:
    """收集文件 import 语句的规范名集合（含 from 形式，如 ``app.core.redis.get_redis_client``）。"""
    tree = ast.parse((BACKEND_ROOT / rel_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


def _is_redis_client_usage(imports: set[str], source: str) -> bool:
    """判断 import 集合/源码是否构成 Redis 客户端直接使用。

    - 直接 import ``redis`` 库（含 ``redis``/``redis_lib`` 顶层名）；
    - 从 ``app.core.redis`` 导入客户端工厂 ``get_redis_client``；
    - 源码中调用 ``get_redis_client``。
    """
    redis_lib = any(name == "redis" or name.startswith("redis.") for name in imports)
    factory_import = any(
        name.startswith("app.core.redis.") and name.endswith("get_redis_client") for name in imports
    )
    call = "get_redis_client" in source
    return redis_lib or factory_import or call


def _function_source(rel_path: str, func_name: str) -> str:
    """提取文件内指定函数/方法（含类方法）的源码片段。"""
    source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{func_name} 未在 {rel_path} 中找到")


def test_business_layers_never_use_redis_client() -> None:
    """服务/仓储/worker 三个业务状态层不得出现任何 Redis 客户端使用。

    任务状态、租户边界等业务真相只以 PostgreSQL 为准；Redis 不得成为状态来源。
    """
    offenders: list[str] = []
    for rel_dir in _BUSINESS_LAYERS:
        for rel_path in _walk_py(rel_dir):
            source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
            if _is_redis_client_usage(_imports(rel_path), source):
                offenders.append(rel_path)
    assert not offenders, f"业务状态层直接使用 Redis 客户端: {offenders}"


def test_redis_client_confined_to_allowed_modules() -> None:
    """全 app 范围内 Redis 客户端只允许出现在限流/工厂/Celery 装配模块。"""
    offenders: list[str] = []
    for rel_path in _walk_py("app"):
        if rel_path in _ALLOWED_REDIS_MODULES:
            continue
        source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
        if _is_redis_client_usage(_imports(rel_path), source):
            offenders.append(rel_path)
    assert not offenders, f"Redis 客户端出现在未授权模块: {offenders}"


def test_celery_has_no_result_backend() -> None:
    """Celery 不配置结果后端：结果不得承载任务状态（真相只在 PostgreSQL）。"""
    src = (BACKEND_ROOT / "app/workers/celery_app.py").read_text(encoding="utf-8")
    assert "result_backend" not in src, "不得配置 result_backend 承载业务状态"
    assert 'Celery("orionamesh", broker=broker_url())' in src, "Celery 必须装配 broker"
    assert "TASK_QUEUE_NAME" in src, "必须使用核心模块统一任务队列名"


def test_task_state_advances_in_postgres_transaction() -> None:
    """worker 在数据库事务内锁定并推进任务状态，投递仅传输 task_id。"""
    begin = _function_source("app/workers/base.py", "begin_attempt")
    assert "with_for_update" in begin, "worker 必须在事务内锁定任务行（状态真相在 DB）"
    assert "session.commit()" in begin, "任务状态变更必须持久化提交"
    assert "DocumentTaskStatus.QUEUED" in begin, "worker 必须以 queued 状态为准（DB 复查）"
    # 任务状态查询走 repositories 的 PostgreSQL 表。
    list_tasks_src = _function_source("app/services/document_status_service.py", "list_tasks")
    assert "list_for_document" in list_tasks_src, "任务列表必须经 DocumentTaskRepository 查询"
    tasks_repo = (BACKEND_ROOT / "app/repositories/document_tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(tasks_repo)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "list_for_document" in defined, "DocumentTaskRepository 必须提供任务列表查询"
