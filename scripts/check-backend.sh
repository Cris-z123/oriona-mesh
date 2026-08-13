#!/usr/bin/env bash
# 后端质量门禁：锁定安装 → 格式/检查 → 类型检查 → 测试。
# 命令序列与 CI（.github/workflows/ci.yml，Phase 7）保持一致；任一步失败即退出非零。
set -euo pipefail

cd "$(dirname "$0")/../backend"

echo "==> uv lock --check"
uv lock --check

echo "==> uv sync --locked"
uv sync --locked

echo "==> ruff format --check"
uv run ruff format --check .

echo "==> ruff check"
uv run ruff check .

echo "==> pyright"
uv run pyright

echo "==> pytest"
uv run pytest

echo "==> check-backend OK"
