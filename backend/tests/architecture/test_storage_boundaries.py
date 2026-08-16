"""存储边界架构测试（T088 / plan.md 关键设计决策、data-model.md 本地卷边界）。

- 业务层（services 除 ``file_storage.py`` 外、workers）不拼绝对路径：不直接使用
  ``LocalStorage``、不出现 ``os.path.join``、不访问 ``storage_root`` 根路径；
  文件路径操作只允许在 ``app/infrastructure/storage/local.py`` 与
  ``app/services/file_storage.py``；
- 数据库只保存相对对象键：``Document.storage_path`` 为 String 列，服务层写入的
  对象键来自可推导的相对键生成器（``final_object_key``），``FileStorage`` 接口
  以相对键工作且不向调用方返回绝对路径；
- 路径逃逸防护：``LocalStorage._safe_abs_path`` 拒绝绝对路径、``..`` 与解析后
  逃出根目录的对象键，所有通用对象读写删除必经该校验。

自包含：纯 ``ast`` 源码解析，不依赖数据库/Redis。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

BACKEND_ROOT = Path(__file__).resolve().parents[2]

LOCAL_STORAGE = "app/infrastructure/storage/local.py"
FILE_STORAGE_SERVICE = "app/services/file_storage.py"
DOCUMENT_MODEL = "app/models/document.py"
DOCUMENT_SERVICE = "app/services/document_service.py"

# 允许触碰文件路径/根目录的模块：存储适配器与存储门面本身。
_ALLOWED_PATH_MODULES = {LOCAL_STORAGE, FILE_STORAGE_SERVICE}
# 业务层：禁止 LocalStorage 直接使用、os.path.join 与 storage_root 访问。
_BUSINESS_LAYERS = ("app/services", "app/workers")


def _walk_py(rel_dir: str) -> list[str]:
    """返回目录（相对 backend 根）下全部 .py 文件相对路径。"""
    base = BACKEND_ROOT / rel_dir
    if not base.is_dir():
        return []
    return sorted(str(p.relative_to(BACKEND_ROOT)).replace("\\", "/") for p in base.rglob("*.py"))


def _imports(rel_path: str) -> set[str]:
    """收集文件 import 语句的规范名集合（含 from 形式）。"""
    tree = ast.parse((BACKEND_ROOT / rel_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


def _attribute_accesses(rel_path: str) -> set[str]:
    """收集文件内出现的属性访问链末段名（如 ``os.path.join`` 的 ``join``）。"""
    tree = ast.parse((BACKEND_ROOT / rel_path).read_text(encoding="utf-8"))
    accesses: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            accesses.add(node.attr)
    return accesses


def _function_source(rel_path: str, func_name: str) -> str:
    """提取文件内指定函数/方法（含类方法）的源码片段。"""
    source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{func_name} 未在 {rel_path} 中找到")


def test_business_layers_never_build_absolute_paths() -> None:
    """业务层不拼绝对路径：不 import LocalStorage、无 os.path.join、无 storage_root。"""
    offenders_local: list[str] = []
    offenders_join: list[str] = []
    offenders_root: list[str] = []
    for rel_dir in _BUSINESS_LAYERS:
        for rel_path in _walk_py(rel_dir):
            if rel_path == FILE_STORAGE_SERVICE:
                continue  # 存储门面是唯一允许触碰 LocalStorage/根的业务模块
            imports = _imports(rel_path)
            if any(name.endswith(".LocalStorage") or name == "LocalStorage" for name in imports):
                offenders_local.append(rel_path)
            source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
            if "os.path.join" in source:
                offenders_join.append(rel_path)
            if "storage_root" in _attribute_accesses(rel_path):
                offenders_root.append(rel_path)
    assert not offenders_local, f"业务层直接 import LocalStorage: {offenders_local}"
    assert not offenders_join, f"业务层用 os.path.join 拼接路径: {offenders_join}"
    assert not offenders_root, f"业务层访问持久卷根 storage_root: {offenders_root}"


def test_path_operations_confined_to_storage_modules() -> None:
    """路径操作（LocalStorage 使用/根读取）只允许在存储适配器与存储门面中。"""
    offenders: list[str] = []
    for rel_path in _walk_py("app"):
        if rel_path in _ALLOWED_PATH_MODULES:
            continue
        imports = _imports(rel_path)
        if any(name.endswith(".LocalStorage") or name == "LocalStorage" for name in imports):
            offenders.append(rel_path)
    assert not offenders, f"LocalStorage 被存储模块之外使用: {offenders}"


def test_document_storage_path_is_relative_string_column() -> None:
    """数据库只保存相对对象键：storage_path 为 String 列（禁止绝对路径）。"""
    model_source = (BACKEND_ROOT / DOCUMENT_MODEL).read_text(encoding="utf-8")
    assert "storage_path: Mapped[str] = mapped_column(String(500)" in model_source, (
        "storage_path 必须是 String 列（相对对象键），禁止绝对路径"
    )


def test_service_writes_derived_relative_object_keys() -> None:
    """服务层写入 storage_path 的对象键来自相对键生成器，且生成器不引用根目录。"""
    persist = _function_source(DOCUMENT_SERVICE, "_persist_batch")
    assert "final_object_key(batch_id, doc_id)" in persist, (
        "资料写入必须使用可推导的相对对象键生成器"
    )
    for fn in ("temp_object_key", "final_object_key"):
        key_fn = _function_source(LOCAL_STORAGE, fn)
        assert key_fn.startswith("def "), f"{fn} 必须存在"
        assert "self.root" not in key_fn, f"{fn} 不得拼接持久卷根目录"
        # 相对键：以 ``tmp/`` 或 ``obj/`` 开头（不带前导斜杠，不带根路径）。
        assert 'return f"tmp/' in key_fn or 'return f"obj/' in key_fn, f"{fn} 必须返回根内相对键"


def test_file_storage_api_works_with_relative_keys_only() -> None:
    """FileStorage 接口以相对键工作：put/get 参数为对象键，不向调用方返回绝对路径。"""
    for method in ("write_object", "read_object", "delete_object"):
        src = _function_source(FILE_STORAGE_SERVICE, method)
        assert "object_key: str" in src, f"FileStorage.{method} 必须以字符串相对键为参数"
        assert "self.storage" in src, f"FileStorage.{method} 必须委托 LocalStorage 适配器"
    # storage_root 只作为只读属性存在（门面调试用途），业务调用方不依赖绝对路径：
    # 全部业务层已断言不访问 storage_root（test_business_layers_never_build_absolute_paths）。


def test_local_storage_blocks_path_escape() -> None:
    """路径逃逸防护：拒绝绝对路径、.. 与解析后逃出根目录的对象键。

    对应 data-model.md 本地卷边界：对象键必须落在持久卷根目录内。
    """
    guard = _function_source(LOCAL_STORAGE, "_safe_abs_path")
    assert "is_absolute()" in guard, "必须拒绝绝对路径对象键"
    assert '".."' in guard, "必须拒绝 .. 目录穿越"
    assert "is_relative_to" in guard, "必须校验解析后路径仍在根目录内"
    # 通用对象读写/删除全部经该校验，杜绝绕过。
    for method in ("write_object", "read_object", "delete_object"):
        src = _function_source(LOCAL_STORAGE, method)
        assert "_safe_abs_path" in src, f"LocalStorage.{method} 必须经路径逃逸校验"
