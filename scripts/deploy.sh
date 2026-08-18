#!/usr/bin/env bash
# OrionaMesh GitHub Release 部署脚本。
#
# 在已校验并解压的 release 目录中执行：
#   sudo bash scripts/deploy.sh /opt/orionamesh
#
# release 目录必须包含 images/backend.tar、images/frontend.tar、release.env、release.files.sha256、
# deploy/compose/compose.yaml、deploy/nginx/nginx.conf。服务器只 docker load，绝不
# 在此脚本中 docker build 或 docker pull 应用镜像。
set -euo pipefail

RELEASE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="${1:-/opt/orionamesh}"
COMPOSE_SOURCE="$RELEASE_ROOT/deploy/compose/compose.yaml"
NGINX_SOURCE="$RELEASE_ROOT/deploy/nginx/nginx.conf"
IMAGE_MANIFEST="$RELEASE_ROOT/release.env"
FILE_CHECKSUMS="$RELEASE_ROOT/release.files.sha256"

for required in "$COMPOSE_SOURCE" "$NGINX_SOURCE" "$IMAGE_MANIFEST" \
    "$FILE_CHECKSUMS" "$RELEASE_ROOT/images/backend.tar" "$RELEASE_ROOT/images/frontend.tar"; do
    if [[ ! -r "$required" ]]; then
        echo "错误：发布包不完整，缺少 $required" >&2
        exit 1
    fi
done

if [[ ! -f "$INSTALL_ROOT/.env" ]]; then
    echo "错误：缺少 $INSTALL_ROOT/.env。请从 deploy/compose/.env.example 创建并填写服务器秘密。" >&2
    exit 1
fi

echo "==> 校验发布包内文件"
(cd "$RELEASE_ROOT" && sha256sum -c "$(basename "$FILE_CHECKSUMS")")

COMPOSE_TARGET="$INSTALL_ROOT/deploy/compose/compose.yaml"
mkdir -p "$INSTALL_ROOT/deploy/compose" "$INSTALL_ROOT/deploy/nginx"
install -m 0644 "$COMPOSE_SOURCE" "$COMPOSE_TARGET"
install -m 0644 "$NGINX_SOURCE" "$INSTALL_ROOT/deploy/nginx/nginx.conf"

echo "==> 导入已验证的 backend/frontend 镜像"
docker image load -i "$RELEASE_ROOT/images/backend.tar"
docker image load -i "$RELEASE_ROOT/images/frontend.tar"

compose() {
    ORIONAMESH_NGINX_CONFIG="$INSTALL_ROOT/deploy/nginx/nginx.conf" docker compose \
        --project-directory "$INSTALL_ROOT" \
        --env-file "$INSTALL_ROOT/.env" \
        --env-file "$IMAGE_MANIFEST" \
        -f "$COMPOSE_TARGET" \
        "$@"
}

# 镜像引用只由已校验发布包的 release.env 提供：拒绝 .env 定义，防止误填 latest 或旧引用。
if grep -Eq '^[[:space:]]*(BACKEND_IMAGE|FRONTEND_IMAGE)[[:space:]]*=' "$INSTALL_ROOT/.env"; then
    echo "错误：$INSTALL_ROOT/.env 不得定义 BACKEND_IMAGE/FRONTEND_IMAGE；镜像引用只由已校验发布包的 release.env 提供。" >&2
    exit 1
fi

# 宿主环境变量优先级高于两个 --env-file，可同时压过 .env 与 release.env；
# 用 docker compose config 断言最终插值出的镜像引用与已校验清单一致。
echo "==> 校验最终镜像引用与已校验清单一致"
resolved_images="$(compose config | grep -E '^[[:space:]]+image: (orionamesh-backend|orionamesh-frontend):' | awk '{print $2}' | sort -u)"
expected_images="$(printf '%s\n%s\n' \
    "$(sed -n 's/^BACKEND_IMAGE=//p' "$IMAGE_MANIFEST")" \
    "$(sed -n 's/^FRONTEND_IMAGE=//p' "$IMAGE_MANIFEST")" \
    | sort -u)"
if [ "$resolved_images" != "$expected_images" ]; then
    echo "错误：最终 Compose 镜像引用与清单不一致。" >&2
    echo "  实际: $(echo "$resolved_images" | tr '\n' ' ')" >&2
    echo "  期望: $(echo "$expected_images" | tr '\n' ' ')" >&2
    echo "  检查宿主 shell 是否导出了 BACKEND_IMAGE/FRONTEND_IMAGE，或 .env 被修改。" >&2
    exit 1
fi

# 基础设施镜像（PostgreSQL/Redis/Nginx）是首次部署才获取的 Docker Hub 镜像，
# 本机缺失时允许拉取（--pull missing）；应用镜像必须来自上方已 load 的 Release 包。
echo "==> 启动 PostgreSQL 与 Redis 并等待健康（不会发布数据库/缓存端口）"
compose up -d --no-build --pull missing --wait --wait-timeout 300 postgres redis

echo "==> 运行 one-off 数据库迁移；失败时不更新 API、worker、前端或 nginx"
# run --rm --no-deps：独立 one-off 容器，可靠传播迁移退出码（docker compose wait 对快速
# 成功退出误报失败），且不触碰基础设施生命周期（--exit-code-from 隐含
# --abort-on-container-exit，会管理依赖容器，跨版本停止语义不确定）。
# 迁移容器随后被 --rm 移除；api/worker 的 depends_on service_completed_successfully 会在
# 下次 up 时自动补跑幂等的 alembic upgrade head，作为顺序保障。
if ! compose run --rm --no-deps --pull never migrate; then
    echo "错误：数据库迁移失败；现有应用容器未被本次脚本替换。" >&2
    exit 1
fi

echo "==> 启动并等待应用健康检查"
compose up -d --no-build --pull never --wait --wait-timeout 300 api worker frontend

echo "==> 启动 Nginx（与基础设施同策略：本机缺失时允许拉取）"
compose up -d --no-build --pull missing --wait nginx

echo "==> 部署完成"
compose ps
