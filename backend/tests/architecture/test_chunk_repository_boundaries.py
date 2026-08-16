"""chunks 仓储边界架构测试（T088 / data-model.md 片段读取边界、宪章 V）。

权威依据：
- data-model.md：``chunks`` 的所有读取必须经过统一 ``ChunkRepository``；除迁移和
  测试夹具外，路由、服务与 worker 不得直接执行该表的 SQL 或 ORM 查询；
- 宪章 V：任何业务服务、worker 或路由不得直接查询 ``chunks``；检索读取强制
  JOIN documents 并过滤当前用户、知识库、完成状态与当前版本；
- plan.md 结构决策：``chunks`` 的检索、引用活表读取和流水线校验统一收口到
  ``backend/app/repositories/chunks.py``。

边界定义（本测试的合理边界）：
- 禁止的是对 ``chunks`` 表（``Chunk`` 模型）的“直接查询/读取”，即 select/delete/
  update/session.get 语句与查询列引用；为写入而构造 ORM 实例（如 embed 阶段
  拼装 ``Chunk`` 对象交给 ``ChunkRepository.replace_for_version`` 直写）不构成
  读取，允许保留在 worker 内；
- 删除清理（``delete_cleanup``，plan.md 决策 7）是独立于流水线的删除编排：资料
  已进入 ``deleting`` 后 fencing 会拒绝一切流水线写入，该 worker 在删除事务内
  直接清理解析结果/草稿行属设计行为（``chunks`` 正式片段仍经
  ``ChunkRepository.delete_for_document`` 整份移除），本测试以显式例外收窄。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# 禁止直接查询的 chunk 家族模型名（chunks 表实体为 Chunk；草稿/解析结果为
# 同模块内的流水线中间表，读取同样只经各自仓储）。
_CHUNK_MODELS = {"Chunk", "DocumentChunkDraft", "DocumentParseResult"}

# 允许直接构造 SQL 语句的目录与文件：
# - app/repositories/：统一仓储实现；
# - app/workers/document_delete_cleanup.py：删除清理编排（资料 deleting 后
#   fencing 拒绝流水线写入，清理行数据只能在删除路径直接执行）。
_ALLOWED_DIRECT_SQL = ("repositories",)
_ALLOWED_DELETE_CLEANUP_WORKER = "workers/document_delete_cleanup.py"


def _py_files() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - 源码语法错误属实现缺陷
        pytest.fail(f"{path.relative_to(APP_ROOT)} 语法解析失败: {exc}")


def _query_usages(tree: ast.AST, model_names: set[str]) -> list[str]:
    """收集把模型名当作 SQL 语句目标或查询列使用的用法（不含实例构造）。"""
    usages: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in ("select", "delete", "update"):
                for arg in node.args:
                    args = arg.elts if isinstance(arg, ast.Tuple) else [arg]
                    for item in args:
                        if isinstance(item, ast.Name) and item.id in model_names:
                            usages.append(f"{name}({item.id}) 第 {node.lineno} 行")
            elif name == "get" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in model_names:
                    usages.append(f"session.get({first.id}) 第 {node.lineno} 行")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in model_names
        ):
            usages.append(f"{node.value.id}.{node.attr} 第 {node.lineno} 行")
    return usages


def test_chunk_repository_defined_only_in_repositories() -> None:
    """``ChunkRepository`` 类只允许定义在 app/repositories/chunks.py。"""
    definitions = [
        path.relative_to(APP_ROOT).as_posix()
        for path in _py_files()
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.ClassDef) and node.name == "ChunkRepository"
    ]
    assert definitions == ["repositories/chunks.py"], (
        f"ChunkRepository 必须唯一定义于 repositories/chunks.py，实际: {definitions}"
    )


def test_api_services_workers_do_not_directly_query_chunks() -> None:
    """api/services/workers 不得对 ``Chunk`` 模型（chunks 表）执行 SQL 或 ORM 查询。

    AST 检测 select/delete/update/session.get 目标与查询列属性引用；允许
    ``Chunk(...)`` 实例构造（写透传仓储，如 embed 阶段直写前拼装对象）。
    """
    forbidden_dirs = ("api", "services", "workers")
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        if not any(rel.parts[0] == d for d in forbidden_dirs):
            continue
        usages = _query_usages(_parse(path), {"Chunk"})
        if usages:
            violations.append(f"{rel.as_posix()}:\n  " + "\n  ".join(usages))
    assert not violations, (
        "路由/服务/worker 不得直接查询 chunks 表（data-model.md 片段读取边界）:\n"
        + "\n".join(violations)
    )


def test_direct_sql_on_chunk_family_confined_to_repositories_and_delete_cleanup() -> None:
    """chunk 家族（Chunk/草稿/解析结果）的直接 SQL 只允许出现在仓储与删除清理路径。

    该断言把 plan.md 决策 7 的删除编排例外显式收窄：流水线阶段 worker 的读取与
    写入必须全部经仓储；只有 delete_cleanup 可在删除事务内直接清理行数据。
    """
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        rel_posix = rel.as_posix()
        usages = _query_usages(_parse(path), _CHUNK_MODELS)
        if not usages:
            continue
        allowed = any(rel.parts[0] == d for d in _ALLOWED_DIRECT_SQL) or (
            rel_posix == _ALLOWED_DELETE_CLEANUP_WORKER
        )
        if not allowed:
            violations.append(f"{rel_posix}:\n  " + "\n  ".join(usages))
    assert not violations, (
        "chunk 家族表的直接 SQL 只能出现在 app/repositories/ 与删除清理 worker:"
        + "\n".join(violations)
    )


def test_retrieval_reads_through_chunk_repository() -> None:
    """检索服务通过 ``ChunkRepository`` 读取，不得直接 import chunks 模型。"""
    retrieval = APP_ROOT / "services" / "retrieval_service.py"
    source = retrieval.read_text(encoding="utf-8")
    assert "from app.repositories.chunks import" in source, (
        "retrieval_service 必须从 repositories.chunks 导入 ChunkRepository/RetrievalChunk"
    )
    assert "from app.models.chunk import" not in source, (
        "retrieval_service 不得直接导入 chunks 模型"
    )
    tree = _parse(retrieval)
    usages = _query_usages(tree, {"Chunk"})
    assert not usages, f"retrieval_service 存在对 Chunk 模型的直接查询: {usages}"


def test_citation_live_source_reads_through_repository() -> None:
    """引用活表读取（live 来源）统一收口到 ChunkRepository.get_live_source。"""
    citation = APP_ROOT / "services" / "citation_service.py"
    source = citation.read_text(encoding="utf-8")
    assert "from app.repositories.chunks import" in source
    assert "get_live_source" in source, "引用活表读取必须经 ChunkRepository.get_live_source"
    assert "from app.models.chunk import" not in source, "citation_service 不得导入 chunks 模型"


def test_pipeline_workers_read_and_write_through_repositories() -> None:
    """流水线 worker 的草稿/解析结果/正式片段读写全部经仓储并携带 attempt_id。"""
    workers_root = APP_ROOT / "workers"

    embed = (workers_root / "document_embed.py").read_text(encoding="utf-8")
    assert "from app.repositories.chunk_drafts import ChunkDraftRepository" in embed
    assert "from app.repositories.chunks import ChunkRepository" in embed
    assert "list_for_version(" in embed, "embed 阶段必须经仓储读取草稿"
    assert "replace_for_version(" in embed and "attempt_id=attempt.id" in embed, (
        "embed 阶段正式片段写入必须经 ChunkRepository.replace_for_version(attempt_id=...)"
    )

    chunk = (workers_root / "document_chunk.py").read_text(encoding="utf-8")
    assert "from app.repositories.chunk_drafts import ChunkDraftRepository" in chunk
    assert "from app.repositories.parse_results import ParseResultRepository" in chunk
    assert "latest_for_version(" in chunk, "chunk 阶段必须经仓储读取解析结果"
    assert "replace_for_version(" in chunk and "attempt_id=attempt.id" in chunk, (
        "chunk 阶段草稿写入必须经 ChunkDraftRepository.replace_for_version(attempt_id=...)"
    )

    parse = (workers_root / "document_parse.py").read_text(encoding="utf-8")
    assert "from app.repositories.parse_results import ParseResultRepository" in parse
    assert "save(" in parse and "attempt_id=attempt.id" in parse, (
        "parse 阶段必须经 ParseResultRepository.save(attempt_id=...) 写入"
    )
