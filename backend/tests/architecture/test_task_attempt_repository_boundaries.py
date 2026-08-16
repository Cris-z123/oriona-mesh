"""任务尝试仓储边界架构测试（T088 / data-model.md attempt 规则、宪章 I/III）。

权威依据：
- data-model.md：attempt 创建必须事务锁定父任务并复制/校验租户边界
  （``user_id``、``knowledge_base_id``、``document_id``、``document_version``），
  四个冗余边界由数据库五列复合外键作最后一道一致性约束，完整性异常安全转换为
  资源冲突错误；attempt 的读写必须经带当前 ``user_id`` 条件的任务尝试仓储完成；
- 宪章 I：按 ID 查询必须同时过滤当前用户，不得做全局存在性探测（不得区分
  “不存在”与“属于其他用户”）。

边界定义（与现有集成测试契约一致）：
- ``create_for_task`` 的父任务锁定查询是 worker 内部 fenced 事务（task_id 来自
  任务行本身而非用户输入），边界一致性校验（``provided != authoritative`` →
  ``20008/409``，集成测试契约“任一边界不匹配即拒绝”）阻止跨租户写入；
- ``_next_attempt_no`` 是对已锁定父任务的 ``attempt_no`` 聚合编号（``func.max``），
  非按 ID 的存在性探测；
- 面向用户的按 ID 读取（``get_for_user``）必须同时过滤 ``user_id``，未命中统一
  ``20007/404``。
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.repositories.document_task_attempts import (
    _TENANT_BOUNDARY_FIELDS,
    DocumentTaskAttemptRepository,
)

pytestmark = pytest.mark.architecture

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# 租户/版本冗余边界字段（父任务 → attempt 复制校验，data-model.md）。
_EXPECTED_BOUNDARY_FIELDS = ("user_id", "knowledge_base_id", "document_id", "document_version")

# 面向用户的读取方法（必须固定过滤当前用户）。
_USER_SCOPED_READ_METHODS = ("get_for_user", "get_open_for_task", "list_for_task")


def test_tenant_boundary_fields_constant() -> None:
    """冗余边界字段常量必须恰好覆盖 user_id/knowledge_base_id/document_id/document_version。"""
    assert _TENANT_BOUNDARY_FIELDS == _EXPECTED_BOUNDARY_FIELDS


def test_create_for_task_locks_parent_task_with_for_update() -> None:
    """attempt 创建必须在同一事务用 ``with_for_update`` 锁定父任务（data-model.md）。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.create_for_task)
    assert "with_for_update()" in source, "create_for_task 必须事务锁定父任务"


def test_create_for_task_copies_boundaries_from_parent_task() -> None:
    """创建 attempt 时四个冗余边界必须从父任务复制（不得信任调用方独立值）。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.create_for_task)
    for field in _EXPECTED_BOUNDARY_FIELDS:
        assert f"{field}=task.{field}" in source, f"create_for_task 必须从父任务复制边界 {field}"


def test_create_for_task_validates_provided_boundaries_against_parent() -> None:
    """创建 attempt 必须校验调用方提供边界与父任务一致（不一致拒绝创建）。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.create_for_task)
    assert "authoritative" in source, "必须从父任务取出权威边界"
    assert "provided != authoritative" in source, "必须显式比较调用方边界与父任务边界"


def test_create_for_task_converts_integrity_error_to_conflict() -> None:
    """数据库复合外键完整性异常必须安全转换为 ``20008/409`` 冲突错误。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.create_for_task)
    assert "IntegrityError" in source, "必须捕获数据库完整性异常（最后一道一致性约束）"
    assert "rollback()" in source, "完整性异常必须回滚事务"
    assert "ApiError(20008" in source, "完整性异常必须收敛为 20008/409"


def test_read_methods_scope_by_current_user() -> None:
    """读取方法必须固定过滤当前用户（user_id= 条件），禁止无用户条件的读取。"""
    for method_name in _USER_SCOPED_READ_METHODS:
        method = getattr(DocumentTaskAttemptRepository, method_name)
        source = inspect.getsource(method)
        assert "DocumentTaskAttempt.user_id == user_id" in source, (
            f"{method_name} 必须按 user_id 过滤（宪章 I 租户隔离）"
        )


def test_by_id_read_filters_user_id_not_a_global_probe() -> None:
    """按 ID 读取（get_for_user）必须同时携带 id 与 user_id 条件：未命中不全局探测。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.get_for_user)
    assert "DocumentTaskAttempt.id == attempt_id" in source, "必须按 ID 定位行"
    assert "DocumentTaskAttempt.user_id == user_id" in source, (
        "按 ID 读取必须同时过滤当前用户（禁止全局存在性探测）"
    )


def test_no_unscoped_session_get_by_primary_key() -> None:
    """attempt 仓储不得使用 ``session.get`` 这类无租户条件的按主键读取。"""
    module_path = APP_ROOT / "repositories" / "document_task_attempts.py"
    source = module_path.read_text(encoding="utf-8")
    assert "session.get(" not in source, "按 ID 读取必须经带 user_id 条件的 select"


def test_attempt_no_numbering_is_scoped_aggregate_not_existence_probe() -> None:
    """``_next_attempt_no`` 是对已锁定父任务的聚合编号，不是按 ID 的存在性探测。"""
    source = inspect.getsource(DocumentTaskAttemptRepository._next_attempt_no)
    assert "func.max(" in source, "attempt_no 编号必须是聚合计算（不读取行本身）"
    assert "select(" in source
    assert "DocumentTaskAttempt.task_id == task_id" in source, "聚合必须限定在父任务范围内"


def test_every_document_task_attempt_select_in_read_path_has_user_scope() -> None:
    """面向用户的读取路径中，所有对 attempt 表的 select 都必须带 user_id 条件。"""
    module_path = APP_ROOT / "repositories" / "document_task_attempts.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))
    flagged: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name in ("_next_attempt_no",):
            continue  # 聚合编号（父任务已锁定并校验），非存在性探测
        node_source = ast.get_source_segment(source, node)
        if node_source is None:
            continue
        # 只要函数内出现对 attempt 表的 select，就必须同时出现 user_id 过滤。
        if "select(DocumentTaskAttempt" in node_source:
            if "DocumentTaskAttempt.user_id == user_id" not in node_source:
                flagged.append(node.name)
    assert not flagged, f"attempt 表读取必须固定过滤当前用户（宪章 I）: {', '.join(flagged)}"
