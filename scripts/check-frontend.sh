#!/usr/bin/env bash
# 前端质量门禁（T101）：根锁文件安装 → lint → Prettier → 类型检查 → 单测 → e2e。
# 命令序列与 CI（.github/workflows/ci.yml，T102）保持一致；任一步失败即退出非零。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> pnpm install --frozen-lockfile（根唯一锁文件）"
pnpm install --frozen-lockfile

echo "==> pnpm lint"
pnpm lint

echo "==> pnpm format:check"
pnpm format:check

echo "==> pnpm typecheck"
pnpm typecheck

echo "==> pnpm test（vitest）"
pnpm test

if [ -f "$ROOT/frontend/playwright.config.ts" ]; then
  echo "==> pnpm test:e2e（playwright）"
  pnpm test:e2e
else
  echo "==> pnpm test:e2e 跳过（playwright.config.ts 由阶段 10 的 T119 提供）"
fi

echo "==> check-frontend OK"
