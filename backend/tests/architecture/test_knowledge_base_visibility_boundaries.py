"""知识库可见性边界架构测试（T088 / plan.md 关键设计决策、data-model.md 删除知识库）。

- 知识库 DELETE 专用锁定查询（``KnowledgeBaseRepository.lock_for_delete``）仅限
  删除编排使用：强制 ``user_id``、带 ``FOR UPDATE`` 锁定特征、可命中
  ``active/deleting/delete_failed``；普通 GET/list/子资源读取不得复用；
- 普通读取边界：``deleting`` 从列表/详情隐藏（统一 ``20002/404``），内容与子
  资源读取只允许 ``active``（``require_active_knowledge_base``）；
- ``delete_failed`` 最小墓碑：只有独立 DTO 分支（``knowledge_base_dto``）命中并
  返回 name/description 为 null、``allowed_actions=["retry_delete"]``；墓碑状态
  只由维护扫描器收敛置位，业务服务不得置 ``delete_failed``。

自包含：纯 ``ast`` 源码解析，不依赖数据库/Redis。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

BACKEND_ROOT = Path(__file__).resolve().parents[2]

KB_REPO = "app/repositories/knowledge_bases.py"
KB_ROUTES = "app/api/v1/routes/knowledge_bases.py"
KB_SERVICE = "app/services/knowledge_base_service.py"
KB_SCHEMAS = "app/api/v1/schemas/knowledge_bases.py"
REPO_BASE = "app/repositories/base.py"
STATUS_SERVICE = "app/services/document_status_service.py"
DOCUMENT_SERVICE = "app/services/document_service.py"
TASK_RECOVERY = "app/workers/task_recovery.py"


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


def _contains_retry_delete_action(source: str) -> bool:
    """源码中是否以列表字面量形式输出 ``retry_delete`` 动作（忽略注释/docstring）。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and any(
            isinstance(elt, ast.Constant) and elt.value == "retry_delete" for elt in node.elts
        ):
            return True
    return False


def test_lock_for_delete_is_dedicated_mutation_query() -> None:
    """DELETE 专用查询：强制 user_id、FOR UPDATE 锁定，可命中 delete_failed。

    对应 data-model.md：``lock_for_delete`` 仅供知识库 DELETE 使用，命中
    active/deleting/delete_failed；不存在统一 ``20002/404``。
    """
    src = _function_source(KB_REPO, "lock_for_delete")
    assert "lock_for_delete(self, kb_id: uuid.UUID, user_id: uuid.UUID)" in src, (
        "lock_for_delete 签名必须强制 user_id 参数"
    )
    assert "with_for_update" in src, "删除编排查询必须带 FOR UPDATE 锁定特征"
    assert "20002" in src, "未命中必须映射 20002/404，禁止全局存在性探测"


def test_only_delete_uses_lock_query_in_service() -> None:
    """服务层只有 delete 使用锁定查询；get/list_for_user/update 只用普通读取。"""
    for method in ("create", "list_for_user", "get", "update"):
        calls = _called_attributes(KB_SERVICE, method)
        assert "lock_for_delete" not in calls, f"KnowledgeBaseService.{method} 不得调用锁定查询"
    calls = _called_attributes(KB_SERVICE, "delete")
    assert "lock_for_delete" in calls, "KnowledgeBaseService.delete 必须使用锁定查询"


def test_routes_read_paths_never_reach_delete_lock_query() -> None:
    """GET 详情/列表/子资源路由只调用只读服务方法，DELETE 路由才进入删除编排。"""
    for route_fn in ("list_knowledge_bases", "get_knowledge_base", "update_knowledge_base"):
        src = _function_source(KB_ROUTES, route_fn)
        assert "lock_for_delete" not in src, f"{route_fn} 不得调用删除专用锁定查询"
        # 只读路由的调用链不得出现任何删除编排入口。
        calls = _called_attributes(KB_ROUTES, route_fn)
        assert calls.isdisjoint({"lock_for_delete", "delete", "stage_document_delete"}), (
            f"{route_fn} 调用链触及删除编排入口: {sorted(calls)}"
        )
        # 且必须经过 KnowledgeBaseService 的只读方法。
        assert calls & {"list_for_user", "get", "update", "create"}, (
            f"{route_fn} 必须调用只读服务方法，实际调用: {sorted(calls)}"
        )
    delete_src = _function_source(KB_ROUTES, "delete_knowledge_base")
    assert "KnowledgeBaseService" in delete_src and "delete" in delete_src
    assert "lock_for_delete" not in delete_src, "锁定查询必须藏在服务层，路由不得直接调用"


def test_deleting_hidden_from_ordinary_reads() -> None:
    """deleting 从列表与详情隐藏：列表排除、详情 20002/404（不全局探测）。"""
    list_src = _function_source(KB_SERVICE, "list_for_user")
    assert "KnowledgeBaseStatus.DELETING" in list_src, "列表必须排除内部 deleting"
    get_src = _function_source(KB_SERVICE, "get")
    assert "KnowledgeBaseStatus.DELETING" in get_src, "详情必须识别 deleting"
    assert "20002" in get_src, "deleting 详情必须映射 20002/404"


def test_subresource_reads_require_active_knowledge_base() -> None:
    """内容与子资源读取只允许 active：deleting/delete_failed 统一 20002/404。"""
    active = _function_source(REPO_BASE, "require_active_knowledge_base")
    assert "KnowledgeBaseStatus.ACTIVE" in active, "子资源读取必须以 active 为门槛"
    assert "20002" in active, "非 active 知识库必须映射 20002/404"
    # 资料列表/详情/任务列表与上传服务必须经 require_active_knowledge_base 守卫。
    for method in ("get_document", "list_documents", "list_tasks"):
        assert "require_active_knowledge_base" in _function_source(STATUS_SERVICE, method), (
            f"DocumentStatusService.{method} 必须要求 active 知识库"
        )
    assert "require_active_knowledge_base" in _function_source(DOCUMENT_SERVICE, "upload")


def test_delete_failed_tombstone_only_via_dto_and_scanner() -> None:
    """delete_failed 最小墓碑：只经 DTO 分支输出，只由维护扫描器收敛置位。

    - ``knowledge_base_dto`` 的 DELETE_FAILED 分支返回 name/description 为 null、
      ``allowed_actions=["retry_delete"]``、delete_error_code=20015；
    - ``retry_delete`` 动作在整个 app 只出现在两个 DTO 文件（资料与知识库）；
    - 业务服务（创建/更新/删除编排）不得把知识库置为 delete_failed——该状态只
      由维护扫描器 ``_converge_knowledge_base_deletion`` 在子资料清理耗尽时收敛。
    """
    dto = _function_source(KB_SCHEMAS, "knowledge_base_dto")
    assert "KnowledgeBaseStatus.DELETE_FAILED" in dto, "DTO 必须包含 delete_failed 分支"
    assert '"name": None' in dto and '"description": None' in dto, "墓碑必须置空名称与描述"
    assert '"allowed_actions": ["retry_delete"]' in dto, "墓碑只允许 retry_delete"
    assert "DELETE_CLEANUP_ERROR_CODE" in dto, "墓碑必须携带 20015 稳定错误码"
    # retry_delete 动作只能由 DTO 转换层输出（资料/知识库两个 schema 文件）。
    for rel in (
        "app/services/knowledge_base_service.py",
        "app/services/document_deletion_service.py",
        "app/repositories/knowledge_bases.py",
        "app/workers/task_recovery.py",
        "app/api/v1/routes/knowledge_bases.py",
    ):
        assert not _contains_retry_delete_action(_source(rel)), (
            f"{rel} 不得输出 retry_delete 动作（只读注释不算）"
        )
    # 置位 delete_failed 只发生在维护扫描器收敛函数；服务层可读但不得写该状态。
    converge = _function_source(TASK_RECOVERY, "_converge_knowledge_base_deletion")
    assert "KnowledgeBaseStatus.DELETE_FAILED" in converge, "扫描器收敛是 delete_failed 置位处"
    for method in ("create", "update", "delete"):
        src = _function_source(KB_SERVICE, method)
        assert "kb.status = KnowledgeBaseStatus.DELETE_FAILED" not in src, (
            f"KnowledgeBaseService.{method} 不得把知识库置为 delete_failed"
        )
