"""后端分层依赖方向架构测试（T088 / 宪章 VIII 职责分离、plan.md 结构决策）。

权威依据：
- 宪章 VIII：路由层负责协议转换和输入输出，服务层负责业务编排，数据访问层负责
  持久化，任务执行层负责后台执行；各层不得承担其他层的业务职责；跨层共享的
  数据结构和接口契约必须集中维护；
- plan.md 结构决策：业务层只能依赖 ``model_gateway`` 端口；限流实现位于
  ``infrastructure/rate_limit``。

方向规则（底层不依赖上层；``app/main.py`` 为组合根，豁免检查）：
- ``models`` 只允许依赖 ``app.db``（ORM 基类）与同层；
- ``db`` 只允许依赖同层；
- ``repositories`` 不得依赖 services/workers 与 api HTTP 层
  （routes/sse/dependencies/router）；
- ``services`` 不得依赖 api HTTP 层；不得依赖 workers——例外：共享提交后投递
  与 attempt 终态适配 ``app.workers.base``（``dispatch_task``/``finish_attempt``，
  plan.md 决策 6 提交后投递由服务编排执行，属文档化合理边界）；
- ``api``（含 ``v1/sse/``、``v1/dependencies/``、middleware）不得依赖 workers；
  基础设施只允许复用 ``app.infrastructure.database.session``、
  ``app.infrastructure.model_gateway.types``，以及限流中间件自身的
  ``app.infrastructure.rate_limit``；
- ``infrastructure`` 不得依赖 services/api/workers/repositories；
- ``workers`` 为执行入口，可依赖 services/repositories/models/core 与共享 DTO，
  不得依赖 api HTTP 层；
- ``core`` 为横切层：共享错误信封（api.middleware.errors）、trace、DTO 与配置
  校验（settings/readiness 读取基础设施配置做启动校验）属合理交叉，但不得依赖
  执行器（workers）、数据访问（repositories/models）与 api HTTP 层。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# api HTTP 层（协议适配）：路由、SSE、依赖注入与组合路由。
_API_HTTP_PREFIXES = (
    "app.api.v1.routes",
    "app.api.v1.sse",
    "app.api.v1.dependencies",
    "app.api.v1.router",
)

# 各层允许豁免的共享模块前缀（文档化合理边界）。
_SERVICES_WORKERS_EXCEPTION = "app.workers.base"
_API_INFRA_ALLOWED = (
    "app.infrastructure.database.session",
    "app.infrastructure.model_gateway.types",
)
_API_MIDDLEWARE_RATE_LIMIT = "app.infrastructure.rate_limit"


def _py_files() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - 源码语法错误属实现缺陷
        pytest.fail(f"{path.relative_to(APP_ROOT)} 语法解析失败: {exc}")


def _module_imports(tree: ast.AST) -> list[str]:
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def _starts_with(imported: str, prefixes: tuple[str, ...]) -> bool:
    return any(imported == p or imported.startswith(p + ".") for p in prefixes)


def _is_forbidden(layer: str, imported: str, rel_posix: str) -> bool:
    """按层判定一条 import 是否违反“底层不依赖上层”的方向规则。"""
    if layer == "models":
        if imported == "app" or imported.startswith("app."):
            return not _starts_with(imported, ("app.db", "app.models"))
    if layer == "db":
        if imported == "app" or imported.startswith("app."):
            return not _starts_with(imported, ("app.db",))
    if layer == "repositories":
        return _starts_with(imported, ("app.services", "app.workers") + _API_HTTP_PREFIXES)
    if layer == "services":
        if _starts_with(imported, ("app.workers",)):
            # 共享提交后投递/attempt 终态适配层（plan.md 决策 6），文档化例外。
            return not _starts_with(imported, (_SERVICES_WORKERS_EXCEPTION,))
        return _starts_with(imported, _API_HTTP_PREFIXES)
    if layer == "api":
        if _starts_with(imported, ("app.workers",)):
            return True
        if _starts_with(imported, ("app.infrastructure",)):
            if _starts_with(imported, _API_INFRA_ALLOWED):
                return False
            # 限流中间件是 rate_limit 基础设施的 HTTP 集成点。
            if _starts_with(imported, (_API_MIDDLEWARE_RATE_LIMIT,)) and rel_posix.startswith(
                "api/middleware/"
            ):
                return False
            return True
    if layer == "infrastructure":
        return _starts_with(
            imported, ("app.services", "app.api", "app.workers", "app.repositories")
        )
    if layer == "workers":
        return _starts_with(imported, _API_HTTP_PREFIXES)
    if layer == "core":
        # 横切层：允许共享错误信封/DTO/配置校验；不得依赖执行器与数据访问层。
        return _starts_with(
            imported,
            ("app.workers", "app.repositories", "app.models", "app.db") + _API_HTTP_PREFIXES,
        )
    return False


def _violations_for(layer: str) -> list[str]:
    """收集指定层的全部方向违规（文件相对路径 + 违规 import）。"""
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        rel_posix = rel.as_posix()
        if rel_posix == "main.py":
            continue  # 组合根：装配全应用，豁免方向检查
        if rel.parts[0] != layer:
            continue
        for imported in _module_imports(_parse(path)):
            if not (imported == "app" or imported.startswith("app.")):
                continue
            if _is_forbidden(layer, imported, rel_posix):
                violations.append(f"{rel_posix} -> {imported}")
    return sorted(set(violations))


def test_models_do_not_import_upper_layers() -> None:
    """模型层只允许依赖 app.db（ORM 基类）与同层，不得 import 上层。"""
    assert _violations_for("models") == []


def test_db_layer_imports_nothing_above() -> None:
    """db 层（SQLAlchemy 基类）不得 import 任何其他 app 模块。"""
    assert _violations_for("db") == []


def test_repositories_do_not_import_services_or_api() -> None:
    """仓储层不得依赖 services/workers 与 api HTTP 层（routes/sse/dependencies）。"""
    assert _violations_for("repositories") == []


def test_services_do_not_import_api_http_layer() -> None:
    """服务层不得依赖 api HTTP 层（routes/sse/dependencies/router）。"""
    assert _violations_for("services") == []


def test_services_only_use_workers_shared_adapter() -> None:
    """服务层对 workers 的唯一允许依赖是 app.workers.base 共享投递/终态适配。"""
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        if rel.parts[0] != "services":
            continue
        for imported in _module_imports(_parse(path)):
            if _starts_with(imported, ("app.workers",)) and not _starts_with(
                imported, (_SERVICES_WORKERS_EXCEPTION,)
            ):
                violations.append(f"{rel.as_posix()} -> {imported}")
    assert violations == []


def test_api_layer_does_not_import_workers_or_infrastructure_implementations() -> None:
    """api 层（含 v1/sse、v1/dependencies、middleware）不得依赖 workers 与基础设施实现。

    允许复用：database.session（会话依赖注入）、model_gateway.types（SSE DTO）
    与限流中间件自身的 infrastructure.rate_limit。
    """
    assert _violations_for("api") == []


def test_infrastructure_does_not_import_upper_layers() -> None:
    """基础设施层不得依赖 services/api/workers/repositories（只允许 core/models）。"""
    assert _violations_for("infrastructure") == []


def test_workers_do_not_import_api_http_layer() -> None:
    """worker 层（执行入口）不得依赖 api HTTP 层；共享 DTO（schemas）允许。"""
    assert _violations_for("workers") == []


def test_core_is_cross_cutting_but_does_not_import_executors_or_data_layer() -> None:
    """core 为横切层：允许共享错误信封/DTO/配置校验，但不得依赖执行器与数据层。"""
    assert _violations_for("core") == []
