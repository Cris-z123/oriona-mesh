"""资料可见性边界架构测试（T088 / plan.md 关键设计决策、data-model.md 删除资料）。

- 资料 DELETE 专用锁定变更查询（``lock_for_delete``）仅限删除编排使用，普通
  GET/list/任务列表不得复用：查询带强制 ``user_id`` 与 ``FOR UPDATE`` 锁定特征，
  只读查询（``get_visible``/``list_visible``）不得加锁；
- 普通读取（列表/详情/任务列表）一律排除内部 ``deleting/deleted`` 状态，公开
  ``status`` 过滤枚举（``PublicDocumentStatus``）不含内部状态，无法被外部参数绕过；
- ``failed/delete_cleanup/20015`` 最小墓碑只能经独立 DTO 分支（
  ``document_dto`` 按 ``is_delete_cleanup_failed`` 判定）命中并只产生
  ``retry_delete``；普通读取查询不做任何 20015 特判。

自包含：纯 ``ast`` 源码解析，不依赖数据库/Redis。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

BACKEND_ROOT = Path(__file__).resolve().parents[2]

DOCUMENTS_REPO = "app/repositories/documents.py"
DOCUMENTS_ROUTES = "app/api/v1/routes/documents.py"
DOCUMENTS_SCHEMAS = "app/api/v1/schemas/documents.py"
DOCUMENTS_MODEL = "app/models/document.py"
STATUS_SERVICE = "app/services/document_status_service.py"
DELETION_SERVICE = "app/services/document_deletion_service.py"


def _source(rel_path: str) -> str:
    """读取 app 内相对路径源码。"""
    return (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")


def _function_source(rel_path: str, func_name: str) -> str:
    """提取文件内指定函数/方法（含类方法）的源码片段。"""
    source = _source(rel_path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{func_name} 未在 {rel_path} 中找到")


def _called_attributes(rel_path: str, func_name: str) -> set[str]:
    """收集指定函数体内（不含装饰器与签名默认值）所有 ``obj.method(...)`` 调用的方法名。"""
    source = _source(rel_path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return {
                child.func.attr
                for stmt in node.body
                for child in ast.walk(stmt)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            }
    raise AssertionError(f"{func_name} 未在 {rel_path} 中找到")


def _assign_value_names(rel_path: str, target: str) -> set[str]:
    """返回模块级赋值 ``target = ...`` 右侧出现的属性名集合（如 DocumentStatus.DELETING）。"""
    tree = ast.parse(_source(rel_path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == target for t in node.targets
        ):
            return {
                child.attr for child in ast.walk(node.value) if isinstance(child, ast.Attribute)
            }
    raise AssertionError(f"模块级赋值 {target} 未在 {rel_path} 中找到")


def test_lock_for_delete_is_dedicated_mutation_query() -> None:
    """删除专用锁定查询：强制 user_id、带 FOR UPDATE 锁定特征，且不复制只读过滤。

    对应 data-model.md：``lock_for_delete`` 仅供资料 DELETE 使用，可命中普通可见
    资料、``deleting`` 与 ``failed/delete_cleanup/20015``；``deleted`` 统一 404。
    """
    src = _function_source(DOCUMENTS_REPO, "lock_for_delete")
    # 必选 user_id（无默认值）：租户边界不可省略。
    assert "lock_for_delete(self, document_id: uuid.UUID, user_id: uuid.UUID)" in src, (
        "lock_for_delete 签名必须强制 user_id 参数"
    )
    # 行级锁定：并发删除/恢复协调必须串行化。
    assert "with_for_update" in src, "删除编排查询必须带 FOR UPDATE 锁定特征"
    # 不得复制只读可见过滤（该查询需要命中 deleting/delete_failed，而非排除它们）。
    assert "_HIDDEN_STATUSES" not in src and "not_in" not in src, "锁定查询不得复用只读可见过滤"


def test_read_queries_never_take_row_locks() -> None:
    """普通读取（列表/详情/任务列表）查询不得使用 FOR UPDATE 行锁。"""
    for method in ("get_visible", "list_visible"):
        src = _function_source(DOCUMENTS_REPO, method)
        assert "with_for_update" not in src, f"{method} 为只读查询，不得加行锁"


def test_get_list_routes_never_reach_delete_lock_query() -> None:
    """GET/list/任务列表路由只调用只读查询链，DELETE 路由才进入锁定变更查询。"""
    for route_fn in ("list_documents", "get_document", "list_document_tasks"):
        src = _function_source(DOCUMENTS_ROUTES, route_fn)
        assert "lock_for_delete" not in src, f"{route_fn} 不得调用删除专用锁定查询"
        assert "DocumentDeletionService" not in src, f"{route_fn} 不得实例化删除编排服务"
        # 只读路由的调用链不得出现任何删除编排/锁定变更入口。
        calls = _called_attributes(DOCUMENTS_ROUTES, route_fn)
        assert calls.isdisjoint({"lock_for_delete", "delete", "stage_document_delete"}), (
            f"{route_fn} 调用链触及删除编排入口: {sorted(calls)}"
        )
        # 且必须经过 DocumentStatusService 的只读方法。
        assert calls & {"list_documents", "get_document", "list_tasks"}, (
            f"{route_fn} 必须调用只读服务方法，实际调用: {sorted(calls)}"
        )
    delete_src = _function_source(DOCUMENTS_ROUTES, "delete_document")
    assert "DocumentDeletionService" in delete_src and "lock_for_delete" not in delete_src
    # 删除编排服务是 lock_for_delete 的唯一使用方。
    assert "lock_for_delete" in _function_source(DELETION_SERVICE, "delete")


def test_read_service_uses_only_visible_queries() -> None:
    """详情/列表/任务列表服务只经可见读取查询命中资料，绝不触碰锁定查询。

    任务/尝试记录经各自的 PostgreSQL 仓储读取（list_for_document/list_for_task），
    但资料行一律走 get_visible（隐藏状态边界）。
    """
    for method in ("get_document", "list_documents", "list_tasks"):
        calls = _called_attributes(STATUS_SERVICE, method)
        assert "lock_for_delete" not in calls, (
            f"DocumentStatusService.{method} 不得调用锁定变更查询"
        )
        assert calls & {"get_visible", "list_visible"}, (
            f"DocumentStatusService.{method} 必须经可见读取查询，实际调用: {sorted(calls)}"
        )


def test_read_queries_exclude_hidden_statuses() -> None:
    """普通读取固定排除内部 deleting/deleted 状态（data-model.md 隐藏状态边界）。"""
    hidden = _assign_value_names(DOCUMENTS_REPO, "_HIDDEN_STATUSES")
    assert {"DELETING", "DELETED"} <= hidden, "模块级 _HIDDEN_STATUSES 必须包含 DELETING/DELETED"
    for method in ("get_visible", "list_visible"):
        src = _function_source(DOCUMENTS_REPO, method)
        assert "_HIDDEN_STATUSES" in src, f"{method} 必须引用隐藏状态过滤"


def test_public_status_filter_cannot_bypass_hidden_statuses() -> None:
    """公开 status 过滤枚举不含内部状态，路由只接受该枚举，外部无法绕过。"""
    schemas = _source(DOCUMENTS_SCHEMAS)
    # PublicDocumentStatus 枚举成员集合：只允许公开状态。
    tree = ast.parse(schemas)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PublicDocumentStatus":
            members: dict[str, str] = {}
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    members[stmt.targets[0].id] = stmt.value.value
            assert members == {
                "PENDING": "pending",
                "QUEUED": "queued",
                "PROCESSING": "processing",
                "COMPLETED": "completed",
                "FAILED": "failed",
            }, f"公开过滤枚举不得含内部状态，实际: {members}"
            break
    else:
        raise AssertionError("PublicDocumentStatus 未定义")
    # 路由列表的 status 查询参数类型必须为 PublicDocumentStatus。
    list_route = _function_source(DOCUMENTS_ROUTES, "list_documents")
    assert "status: PublicDocumentStatus" in list_route, "列表路由 status 参数必须限定公开枚举"


def test_delete_failed_tombstone_only_via_dedicated_dto_branch() -> None:
    """failed/delete_cleanup/20015 最小墓碑只经独立 DTO 分支命中并只产生 retry_delete。

    - 模型单一判定属性 is_delete_cleanup_failed 是墓碑的唯一闸门；
    - document_dto 按该属性分支：墓碑只允许 retry_delete，普通失败才是 delete；
    - 普通读取查询（get_visible/list_visible）不识别 20015，不做任何墓碑特判，
      即墓碑表示路径与普通读取查询完全独立。
    """
    model = _function_source(DOCUMENTS_MODEL, "is_delete_cleanup_failed")
    assert "FAILED" in model and "DELETE_CLEANUP" in model, "墓碑判定必须包含 failed/delete_cleanup"
    assert "DELETE_CLEANUP_ERROR_CODE" in model, "墓碑判定必须包含 20015 稳定错误码"
    dto = _function_source(DOCUMENTS_SCHEMAS, "document_dto")
    assert "is_delete_cleanup_failed" in dto, "DTO 必须按 is_delete_cleanup_failed 分支"
    assert 'allowed_actions = ["retry_delete"]' in dto, "墓碑分支只允许 retry_delete"
    assert 'allowed_actions = ["delete"]' in dto, "普通失败资料才允许 delete"
    for method in ("get_visible", "list_visible"):
        src = _function_source(DOCUMENTS_REPO, method)
        assert "20015" not in src and "DELETE_CLEANUP" not in src, (
            f"{method} 是普通读取查询，不得特判 delete 墓碑"
        )
