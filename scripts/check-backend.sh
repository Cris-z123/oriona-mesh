#!/usr/bin/env bash
# 后端质量门禁：锁定安装 → 格式/检查 → 类型检查 → 全量测试 → 契约校验。
# 命令序列与 CI（.github/workflows/ci.yml，T102）保持一致；任一步失败即退出非零。
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

echo "==> pytest（全量）"
uv run pytest

echo "==> 契约与部署基线校验"
bash "$(dirname "$0")/verify-contracts.sh"

echo "==> check-backend OK"
