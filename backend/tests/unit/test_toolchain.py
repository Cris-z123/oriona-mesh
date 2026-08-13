"""后端工具链可执行性检查（T013）。

验证：uv 锁文件存在且与 pyproject.toml 一致；Ruff、Pyright、pytest 可经 ``uv run``
执行；``scripts/check-backend.sh`` 质量命令脚本存在且包含约定步骤。测试通过子进程调用
工具链，与 CI 使用同一可执行路径。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def test_uv_available() -> None:
    assert shutil.which("uv") is not None, "uv 必须在 PATH 中"


def test_uv_lock_exists() -> None:
    assert (BACKEND_DIR / "uv.lock").is_file(), "backend/uv.lock 缺失"


def test_uv_lock_is_current() -> None:
    result = run(["uv", "lock", "--check"], BACKEND_DIR)
    assert result.returncode == 0, f"uv.lock 与 pyproject.toml 不一致:\n{result.stderr}"


def test_ruff_available() -> None:
    result = run(["uv", "run", "ruff", "--version"], BACKEND_DIR)
    assert result.returncode == 0, result.stderr


def test_pyright_available() -> None:
    result = run(["uv", "run", "pyright", "--version"], BACKEND_DIR)
    assert result.returncode == 0, result.stderr


def test_pytest_collects_tests() -> None:
    result = run(["uv", "run", "pytest", "--collect-only", "-q"], BACKEND_DIR)
    assert result.returncode == 0, f"pytest 收集失败:\n{result.stderr}"


def test_check_backend_script_contains_quality_steps() -> None:
    script = SCRIPTS_DIR / "check-backend.sh"
    assert script.is_file(), "scripts/check-backend.sh 缺失"
    content = script.read_text(encoding="utf-8")
    assert content.startswith("#!/usr/bin/env bash"), "必须是 bash 脚本"
    for step in (
        "uv lock --check",
        "uv sync --locked",
        "ruff format --check",
        "ruff check",
        "pyright",
        "pytest",
    ):
        assert step in content, f"check-backend.sh 缺少步骤: {step}"
