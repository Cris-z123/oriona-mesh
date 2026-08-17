#!/usr/bin/env bash
# OrionaMesh 单机部署脚本（方案 A：服务器本地构建，不依赖 GHCR/GitHub 网络）。
#
# 前提：
#   1. Docker + Compose 插件已安装，Docker Hub 镜像加速已配置（腾讯云 mirror.ccs.tencentyun.com）；
#   2. 代码已位于本仓库目录（git clone / Gitee 中转 / 打包上传均可）；
#   3. deploy/compose/.env 已从 .env.example 复制并填写（缺失时脚本拒绝执行）；
#   4. nginx 反代已按 deploy/nginx/nginx.conf 配置（可选，直连 3000/8000 亦可但会跨域）。
#
# 用法：
#   bash scripts/deploy.sh             # 首次部署（构建并启动全栈）
#   bash scripts/deploy.sh update      # 拉取新代码后重建（git pull --ff-only）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/compose.yaml"

if [ ! -f "$ROOT/deploy/compose/.env" ]; then
    echo "错误：缺少 deploy/compose/.env。请先执行："
    echo "  cp deploy/compose/.env.example deploy/compose/.env"
    echo "  并填写全部必填变量（AUTH_JWT_SECRET_KEY / RATE_LIMIT_SUBJECT_HMAC_KEY /"
    echo "  MODEL_GATEWAY_ENDPOINT(HTTPS) / API_KEY / 改写与生成模型）。"
    exit 1
fi

if [ "${1:-}" = "update" ]; then
    echo "==> 拉取最新代码"
    git -C "$ROOT" pull --ff-only
fi

echo "==> 构建并启动全栈（one-off 迁移成功后才启动 API/worker）"
docker compose -f "$COMPOSE_FILE" up -d --build

echo "==> 等待健康检查"
docker compose -f "$COMPOSE_FILE" up -d --wait --wait-timeout 300

echo "==> 验证就绪"
curl -fsS http://127.0.0.1:8000/ready && echo

echo "==> 部署完成"
echo "  前端：http://<服务器IP>/（经 nginx 同源反代，见 deploy/nginx/nginx.conf）"
echo "  状态：docker compose -f $COMPOSE_FILE ps"
echo "  日志：docker compose -f $COMPOSE_FILE logs -f api worker"
echo "  更新：bash scripts/deploy.sh update"
