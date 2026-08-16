"""模型出口网关边界架构测试（T088 / plan.md 结构决策、model-egress.md 验证要求）。

权威依据：
- plan.md「结构决策」：业务层只能依赖 ``model_gateway`` 端口，供应商 SDK 和凭证
  仅允许出现在 ``infrastructure/model_gateway``；
- model-egress.md「验证要求」：架构测试必须证明只有
  ``backend/app/infrastructure/model_gateway/providers/`` 能导入供应商 SDK、
  LangChain 供应商集成或创建外部模型 HTTP 客户端；
- model-egress.md「调用信封」：业务调用方不得指定供应商凭证，凭证只在网络发送
  边界注入（``providers/factory.py`` 是唯一注入点）。

已知合理例外（配置校验，非凭证注入）：
- ``app/core/settings.py`` 与 ``app/core/readiness.py`` 校验
  ``MODEL_GATEWAY_API_KEY`` 的占用/存在性（禁止复用、就绪校验），不构造任何
  供应商客户端，测试中显式豁免并断言其不注入凭证。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
PROVIDERS_ROOT = APP_ROOT / "infrastructure" / "model_gateway" / "providers"

# 供应商 SDK / 外部模型 HTTP 客户端根模块（model-egress.md 验证要求）。
_SUPPLIER_ROOT_MODULES = (
    "openai",
    "langchain",
    "langchain_openai",
    "langchain_core",
    "httpx",
    "requests",
    "aiohttp",
)

# 供应商客户端构造调用（含 LangChain 包装类）；仅允许出现在 providers/ 目录。
_SUPPLIER_CLIENT_CTORS = (
    "ChatOpenAI",
    "OpenAIEmbeddings",
    "OpenAI",
    "AzureOpenAI",
)

# 凭证注入点：``get_secret_value`` 读取网关凭证的源文件集合。
# 除 model_gateway 内部外，仅允许 app/core 下的配置校验（settings/readiness）。
_CREDENTIAL_READ_ALLOWLIST = {
    "core/settings.py",
    "core/readiness.py",
}


def _py_files() -> list[Path]:
    """app/ 下全部 .py 源文件（跳过 __pycache__ 产物）。"""
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _module_imports(tree: ast.AST) -> list[str]:
    """收集模块级 import 的完整模块路径（相对导入与条件分支忽略）。"""
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def _parse(path: Path) -> ast.AST:
    """解析单文件 AST；语法错误直接以 pytest 失败暴露文件路径。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - 源码语法错误属实现缺陷
        pytest.fail(f"{path.relative_to(APP_ROOT)} 语法解析失败: {exc}")


def _is_under_providers(path: Path) -> bool:
    return PROVIDERS_ROOT in path.parents


def test_provider_sdk_imports_only_in_providers_directory() -> None:
    """供应商 SDK 与外部模型 HTTP 客户端的 import 只允许出现在 providers/ 下。"""
    violations: list[str] = []
    for path in _py_files():
        imports = _module_imports(_parse(path))
        for imported in imports:
            root = imported.split(".", 1)[0]
            if root in _SUPPLIER_ROOT_MODULES and not _is_under_providers(path):
                violations.append(f"{path.relative_to(APP_ROOT)}: import {imported!r}")
    assert not violations, (
        "供应商 SDK / 外部模型 HTTP 客户端只能在 "
        "app/infrastructure/model_gateway/providers/ 内导入（model-egress.md）:\n"
        + "\n".join(violations)
    )


def test_provider_sdk_imports_have_at_least_one_adapter() -> None:
    """providers/ 目录确实存在供应商 SDK 适配器（防止断言空转）。"""
    provider_files = [p for p in _py_files() if _is_under_providers(p)]
    sdk_imports = [
        (path, imp)
        for path in provider_files
        for imp in _module_imports(_parse(path))
        if imp.split(".", 1)[0] in _SUPPLIER_ROOT_MODULES
    ]
    assert sdk_imports, "providers/ 下必须存在供应商 SDK 适配器（openai_compatible.py）"


def test_no_bare_http_client_library_anywhere() -> None:
    """全 app/ 禁止裸 HTTP 客户端库（httpx/requests/aiohttp）直接 import。

    外部模型流量必须经网关内的供应商 SDK 适配器（model-egress.md 唯一出口）。
    """
    violations: list[str] = []
    for path in _py_files():
        for imported in _module_imports(_parse(path)):
            if imported.split(".", 1)[0] in ("httpx", "requests", "aiohttp"):
                violations.append(f"{path.relative_to(APP_ROOT)}: import {imported!r}")
    assert not violations, (
        "app/ 内不得直接导入裸 HTTP 客户端库（httpx/requests/aiohttp）:" + "\n".join(violations)
    )


def test_business_layers_import_gateway_interface_not_providers() -> None:
    """services/workers/api/repositories 只能依赖 model_gateway 端口，不得导入 providers。

    plan.md 结构决策：业务层只能依赖 ``model_gateway`` 端口；供应商适配器
    （providers/ 下实现）对业务层不可见。网关实现本身（model_gateway 内部）
    是端口消费者，允许导入 providers。
    """
    forbidden_prefix = "app.infrastructure.model_gateway.providers"
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        rel_posix = rel.as_posix()
        # 网关内部（service/config 等）是适配器端口的消费者；仅业务层受限。
        if rel_posix.startswith("infrastructure/model_gateway/"):
            continue
        for imported in _module_imports(_parse(path)):
            if imported == forbidden_prefix or imported.startswith(forbidden_prefix + "."):
                violations.append(f"{rel_posix}: import {imported!r}")
    assert not violations, (
        "业务层（services/workers/api/repositories）不得导入模型网关 providers 实现:"
        + "\n".join(violations)
    )


def test_provider_clients_constructed_only_in_providers() -> None:
    """供应商客户端构造调用（ChatOpenAI/OpenAIEmbeddings/OpenAI 等）只在 providers/。"""
    violations: list[str] = []
    for path in _py_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _SUPPLIER_CLIENT_CTORS and not _is_under_providers(path):
                    violations.append(
                        f"{path.relative_to(APP_ROOT)}: 第 {node.lineno} 行调用 {node.func.id}()"
                    )
    assert not violations, (
        "供应商客户端只能在 providers/ 内构造（凭证注入边界，model-egress.md）:"
        + "\n".join(violations)
    )


def test_credentials_read_only_within_model_gateway_or_config_validation() -> None:
    """网关凭证（MODEL_GATEWAY_API_KEY）读取只发生在 model_gateway 内部或 core 配置校验。

    - 业务层（services/workers/api/repositories）源码不得出现 ``api_key`` 标识符；
    - 引用 ``MODEL_GATEWAY_API_KEY`` / ``model_gateway.api_key`` 的文件必须位于
      app/infrastructure/model_gateway/ 或显式豁免的 app/core 配置校验文件。
    """
    business_dirs = ("services", "workers", "api", "repositories", "models")
    api_key_hits: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_ROOT)
        text = path.read_text(encoding="utf-8")
        if any(rel.parts[0] == d for d in business_dirs):
            if "api_key" in text:
                api_key_hits.append(f"{rel}: 含 api_key 引用")
            continue
        if "MODEL_GATEWAY_API_KEY" in text or "model_gateway.api_key" in text:
            rel_posix = rel.as_posix()
            if (
                rel_posix not in _CREDENTIAL_READ_ALLOWLIST
                and not _is_under_providers(path)
                and not rel_posix.startswith("infrastructure/model_gateway/")
            ):
                api_key_hits.append(f"{rel}: 读取网关凭证")
    assert not api_key_hits, (
        "凭证只在 model_gateway 内部读取；core 仅允许配置校验（settings/readiness）:"
        + "\n".join(api_key_hits)
    )


def test_credential_injection_happens_at_provider_factory() -> None:
    """``get_secret_value`` 注入适配器只发生在 providers/factory.py（发送边界）。"""
    factory = PROVIDERS_ROOT / "factory.py"
    assert factory.is_file(), "providers/factory.py 必须存在"
    source = factory.read_text(encoding="utf-8")
    assert "get_secret_value()" in source, "factory 必须从配置读取凭证（发送边界注入）"
    assert "api_key=" in source, "factory 必须把凭证注入适配器构造参数"


def test_model_gateway_service_is_the_only_gateway_entry() -> None:
    """供应商适配器装配（build_provider_adapter）只被网关内部调用。"""
    violations: list[str] = []
    for path in _py_files():
        if _is_under_providers(path):
            continue
        rel = path.relative_to(APP_ROOT)
        if not rel.as_posix().startswith("infrastructure/model_gateway/"):
            text = path.read_text(encoding="utf-8")
            if "build_provider_adapter" in text:
                violations.append(str(rel))
    assert not violations, (
        "供应商适配器工厂只允许在 infrastructure/model_gateway 内部装配:" + "\n".join(violations)
    )
