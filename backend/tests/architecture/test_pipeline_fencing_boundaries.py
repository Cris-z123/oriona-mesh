"""流水线 fencing 与清理路径分离架构测试（T088 / data-model.md 持久化写入边界）。

权威依据：
- data-model.md「持久化写入 fencing」：解析结果、草稿片段、正式 ``chunks``、
  checkpoint 与阶段结果引用的仓储写方法必须接收 ``attempt_id``；每次写入在同一
  数据库事务中锁定 attempt、task 和 document，并校验 attempt/task 均为
  ``running``、版本一致且资料不为 ``deleting/deleted``；条件不满足整笔写入失败；
- plan.md 决策 7：旧版本 ``cleanup`` 不承担删除职责；``delete_cleanup`` 是资料
  删除编排专用任务（``deleting`` 之后 fencing 拒绝流水线写入，删除路径不经过
  fencing）；
- 宪章 II：``cleanup`` 只能清理旧版本派生数据，绝不得影响当前可检索版本。

边界定义：
- 流水线阶段写仓储（parse_results/chunk_drafts/chunks）写方法必须携带
  ``attempt_id`` 并调用 ``validate_attempt_write``；
- ``delete_for_document``（删除清理）是删除编排专用，不携带 attempt_id（资料
  已 deleting，fencing 本就会拒绝）；该路径与普通流水线写入严格分离；
- 初始任务创建（上传批次、attempt 尚不存在）与任务重排（重试编排）不属于
  阶段写方法，不在 fencing 断言范围（data-model.md 上传协调/恢复边界）。
"""

import inspect
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models.enums import DocumentTaskType
from app.repositories.chunk_drafts import ChunkDraftRepository
from app.repositories.chunks import ChunkRepository
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository
from app.repositories.document_tasks import (
    delete_cleanup_idempotency_key,
    stage_idempotency_key,
)
from app.repositories.fencing import validate_attempt_write
from app.repositories.parse_results import ParseResultRepository
from app.services.document_deletion_service import activate_delete_cleanup
from app.services.document_pipeline import _NEXT_TASK_TYPE, DocumentPipelineOrchestrator

pytestmark = pytest.mark.architecture

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# 阶段写仓储写方法（必须携带 attempt_id 并执行 fencing）。
_PIPELINE_WRITE_METHODS: dict[type, tuple[str, ...]] = {
    ParseResultRepository: ("save",),
    ChunkDraftRepository: ("replace_for_version",),
    ChunkRepository: ("replace_for_version",),
}

# 删除清理（无 fencing 的设计路径）方法。
_UNFENCED_DELETE_METHODS: dict[type, tuple[str, ...]] = {
    ChunkRepository: ("delete_for_document",),
}


def test_pipeline_write_methods_carry_attempt_id() -> None:
    """流水线持久化写仓储的写方法签名必须携带 ``attempt_id``（data-model.md）。"""
    for repo_type, methods in _PIPELINE_WRITE_METHODS.items():
        for method_name in methods:
            parameters = inspect.signature(getattr(repo_type, method_name)).parameters
            assert "attempt_id" in parameters, (
                f"{repo_type.__name__}.{method_name} 缺少 attempt_id 参数"
            )


def test_pipeline_write_methods_execute_fencing_validation() -> None:
    """写方法必须在同一事务调用 ``validate_attempt_write`` 执行 fencing 校验。"""
    for repo_type, methods in _PIPELINE_WRITE_METHODS.items():
        for method_name in methods:
            source = inspect.getsource(getattr(repo_type, method_name))
            assert "validate_attempt_write(" in source, (
                f"{repo_type.__name__}.{method_name} 未调用 fencing 校验"
            )


def test_fencing_guard_locks_and_validates_attempt_task_document() -> None:
    """fencing 守卫必须锁定 attempt/task/document 并校验运行态、版本与删除态。"""
    source = inspect.getsource(validate_attempt_write)
    assert "with_for_update()" in source, "fencing 必须在同一事务锁定 attempt/task/document"
    assert "RUNNING" in source, "fencing 必须校验 attempt/task 为 running"
    assert "DELETING" in source, "fencing 必须拒绝 deleting/deleted 资料的写入"
    assert "document_version" in source, "fencing 必须校验版本一致"


def test_attempt_repository_create_locks_parent_task() -> None:
    """attempt 创建锁定父任务：create_for_task 使用 ``with_for_update``（data-model.md）。"""
    source = inspect.getsource(DocumentTaskAttemptRepository.create_for_task)
    assert "with_for_update()" in source, "创建 attempt 必须事务锁定父任务"


def test_delete_for_document_is_unfenced_delete_path() -> None:
    """``delete_for_document``（删除清理）不携带 attempt_id 且不执行 fencing。

    plan.md 决策 7：资料进入 ``deleting`` 后 fencing 拒绝一切流水线写入，删除
    路径是独立编排；该断言确保删除清理不会被误并入普通流水线写入路径。
    """
    for repo_type, methods in _UNFENCED_DELETE_METHODS.items():
        for method_name in methods:
            method = getattr(repo_type, method_name)
            assert "attempt_id" not in inspect.signature(method).parameters, (
                f"{repo_type.__name__}.{method_name} 是删除清理路径，不应携带 attempt_id"
            )
            assert "validate_attempt_write" not in inspect.getsource(method), (
                f"{repo_type.__name__}.{method_name} 不应执行 fencing（删除路径独立编排）"
            )


def test_cleanup_and_delete_cleanup_task_types_are_distinct() -> None:
    """``cleanup``（旧版本清理）与 ``delete_cleanup``（删除资料清理）是独立任务类型。"""
    assert DocumentTaskType.CLEANUP is not DocumentTaskType.DELETE_CLEANUP
    assert DocumentTaskType.CLEANUP.value == "cleanup"
    assert DocumentTaskType.DELETE_CLEANUP.value == "delete_cleanup"


def test_stage_orchestrator_never_chains_delete_cleanup() -> None:
    """阶段编排器只派生 parse→chunk→embed→finalize，不派生 cleanup/delete_cleanup。

    plan.md 决策 6：阶段切换映射只覆盖流水线阶段；cleanup 与 delete_cleanup 均为
    终态阶段，delete_cleanup 由删除编排（DocumentDeletionService）专用创建。
    """
    assert set(_NEXT_TASK_TYPE) == {
        DocumentTaskType.PARSE,
        DocumentTaskType.CHUNK,
        DocumentTaskType.EMBED,
    }
    assert DocumentTaskType.CLEANUP not in _NEXT_TASK_TYPE
    assert DocumentTaskType.DELETE_CLEANUP not in _NEXT_TASK_TYPE
    assert DocumentTaskType.FINALIZE not in _NEXT_TASK_TYPE


def test_complete_stage_terminal_branch_only_for_finalize_and_cleanup() -> None:
    """``complete_stage`` 的终态分支只处理 FINALIZE/CLEANUP，绝不处理 DELETE_CLEANUP。"""
    source = inspect.getsource(DocumentPipelineOrchestrator.complete_stage)
    assert "DocumentTaskType.FINALIZE" in source
    assert "DocumentTaskType.CLEANUP" in source
    assert "DELETE_CLEANUP" not in source, (
        "delete_cleanup 不得进入普通阶段编排（plan.md 决策 7 职责分离）"
    )


def test_delete_cleanup_uses_dedicated_idempotency_key() -> None:
    """删除清理使用独立幂等键函数，与阶段幂等键（stage_idempotency_key）不混用。"""
    stage_source = inspect.getsource(stage_idempotency_key)
    delete_source = inspect.getsource(delete_cleanup_idempotency_key)
    assert "task_type" in stage_source and "{task_type.value}" in stage_source
    assert "delete_cleanup:" in delete_source, "删除清理幂等键必须使用独立前缀"
    assert delete_source != stage_source
    # 同一输入在两条键函数下结果必须不同（互不冲突）。
    document_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert stage_idempotency_key(DocumentTaskType.PARSE, document_id, 1) != (
        delete_cleanup_idempotency_key(document_id, 1, 1)
    )


def test_delete_cleanup_task_created_with_dedicated_type() -> None:
    """删除编排创建的任务必须携带 ``task_type=DELETE_CLEANUP``。"""
    source = inspect.getsource(activate_delete_cleanup)
    assert "task_type=DocumentTaskType.DELETE_CLEANUP" in source
    assert "delete_cleanup_idempotency_key(" in source


def test_cleanup_worker_uses_orchestrator_but_delete_cleanup_worker_does_not() -> None:
    """cleanup 走阶段编排器（终态幂等完成）；delete_cleanup 独立执行，绝不触达编排器。"""
    cleanup_source = (APP_ROOT / "workers" / "document_cleanup.py").read_text(encoding="utf-8")
    delete_source = (APP_ROOT / "workers" / "document_delete_cleanup.py").read_text(
        encoding="utf-8"
    )
    assert "DocumentPipelineOrchestrator" in cleanup_source, "cleanup 必须经阶段编排器完成"
    assert "DocumentPipelineOrchestrator" not in delete_source, (
        "delete_cleanup 是删除编排专用路径，不得进入阶段编排器（plan.md 决策 7）"
    )


def test_fencing_signature_requires_session_and_boundaries() -> None:
    """fencing 校验入口签名保持稳定：session + attempt_id + 租户/版本边界。"""
    parameters = inspect.signature(validate_attempt_write).parameters
    for required in (
        "session",
        "attempt_id",
        "user_id",
        "knowledge_base_id",
        "document_id",
        "document_version",
    ):
        assert required in parameters, f"validate_attempt_write 缺少参数 {required}"
    assert parameters["session"].annotation is Session, "fencing 必须接收 SQLAlchemy Session"
