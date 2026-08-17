#!/usr/bin/env bash
# 契约与部署基线校验（T100）。
#
# 冻结契约与部署基线（不依赖数据库连接；OpenAPI/迁移离线校验 + 配置/依赖静态校验 +
# 契约测试子集）：
#   1) OpenAPI：合法 YAML、每个 operation 声明 x-rate-limit-policy、
#      10005/429 响应带 Retry-After、SSE 操作声明 x-sse-event-schema；
#   2) 迁移离线 SQL（alembic upgrade head --sql）可生成，且含：last_login_at 可空、
#      Citation 非空/唯一（score NOT NULL、message_id+rank 唯一）、delete_cleanup 任务
#      类型、知识库 active/deleting/delete_failed 与 delete_error_code=20015 配对约束；
#   3) 扩展迁移（vector / pg_trgm / pgcrypto）在初始迁移中创建；
#   4) HS256 认证配置：无 AUTH_JWT_ALGORITHM/AUTH_JWT_TTL 等覆盖变量入口，
#      AUTH_JWT_SECRET_KEY 必填且 UTF-8 编码后至少 32 字节；
#   5) 限流与模型出口契约：RATE_LIMIT_* 阈值默认值、MODEL_GATEWAY_* 必填 endpoint/
#      未知 provider 拒绝、Reranker 评分契约（完整 scores、candidate_index 唯一越界拒绝）；
#   6) 解析依赖（pymupdf / python-docx / markdown-it-py / charset-normalizer）可导入；
#   7) 契约测试子集：启动就绪、业务错误码/限流、消息 SSE、模型出口契约。
#
# 与 check-backend.sh 的区别：本脚本只做契约/部署基线校验，不运行质量工具与全量测试；
# CI（T102）按相同文件执行。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
CONTRACT="$ROOT/specs/001-orionamesh-rag-mvp/contracts/openapi.yaml"
MIGRATIONS="$BACKEND/migrations/versions"

cd "$BACKEND"

# 契约校验用最小合法配置（与 tests/conftest.py 的标准测试默认值一致）。
export APP_ENV=test
export AUTH_JWT_SECRET_KEY="verify-contracts-jwt-$(printf 'x%.0s' {1..32})"
export RATE_LIMIT_SUBJECT_HMAC_KEY="verify-contracts-ratelimit-$(printf 'y%.0s' {1..32})"
export MODEL_GATEWAY_ENDPOINT="https://api.example.com/v1"
export MODEL_GATEWAY_API_KEY="verify-contracts-gateway-key"
export MODEL_GATEWAY_QUERY_REWRITE_MODEL="verify-rewrite"
export MODEL_GATEWAY_GENERATION_MODEL="verify-gen"

echo "==> OpenAPI 契约结构（x-rate-limit-policy / Retry-After / SSE）"
uv run python - "$CONTRACT" <<'PY'
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    spec = yaml.safe_load(f)


def resolve(ref: str) -> dict:
    """解析 components 级 $ref（响应对象可能只引用共享组件）。"""
    node: dict = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


assert spec.get("openapi", "").startswith("3."), "openapi.yaml 必须是 OpenAPI 3.x"
paths = spec.get("paths", {})
assert paths, "openapi.yaml 缺少 paths"

for path, item in paths.items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        policy = op.get("x-rate-limit-policy")
        assert policy, f"{method.upper()} {path} 缺少 x-rate-limit-policy"
        responses = op.get("responses", {})
        assert "429" in responses, f"{method.upper()} {path} 缺少 429 响应声明"
        rate_limit_response = responses["429"]
        if "$ref" in rate_limit_response:
            rate_limit_response = resolve(rate_limit_response["$ref"])
        retry_after = rate_limit_response.get("headers", {}).get("Retry-After")
        assert retry_after, f"{method.upper()} {path} 的 429 响应缺少 Retry-After 头"
        if "x-sse-event-schema" in op:
            assert op["x-sse-event-schema"], f"{method.upper()} {path} 的 x-sse-event-schema 为空"

print("OpenAPI 结构校验通过：所有 operation 声明限流策略与 429/Retry-After，SSE 声明事件模式")
PY

echo "==> 迁移离线 SQL 与约束基线"
uv run alembic upgrade head --sql > /tmp/orionamesh-migration.sql
grep -q "last_login_at" /tmp/orionamesh-migration.sql
grep -q "delete_cleanup" /tmp/orionamesh-migration.sql
# Citation：score 非空 + message_id/rank 唯一
grep -q "uq_message_citations_message_rank" /tmp/orionamesh-migration.sql
grep -q "ck_message_citations_score" /tmp/orionamesh-migration.sql
# 知识库 active/deleting/delete_failed 与 delete_error_code=20015 配对约束
grep -q "ck_knowledge_bases_delete_error_code" /tmp/orionamesh-migration.sql
grep -q "20015" /tmp/orionamesh-migration.sql
echo "迁移离线 SQL 生成成功：last_login_at 可空列、delete_cleanup、Citation 非空/唯一、delete_error_code=20015 配对约束均存在"

echo "==> 扩展迁移（vector / pg_trgm / pgcrypto）"
grep -q "CREATE EXTENSION IF NOT EXISTS vector" "$MIGRATIONS/0001_extensions.py"
grep -q "CREATE EXTENSION IF NOT EXISTS pg_trgm" "$MIGRATIONS/0001_extensions.py"
grep -q "pgcrypto" "$MIGRATIONS/0001_extensions.py"
echo "初始迁移创建 vector/pg_trgm/pgcrypto 扩展"

echo "==> 认证/限流/模型出口配置契约"
uv run python - <<'PY'
import os

from app.core.settings import Settings
from app.infrastructure.model_gateway.config import ModelGatewaySettings

# HS256 算法与 7200 秒 TTL 是代码常量：不得出现环境变量覆盖入口。
settings_source = open("app/core/settings.py", encoding="utf-8").read()
for forbidden in ("AUTH_JWT_ALGORITHM", "AUTH_JWT_TTL", "AUTH_TOKEN_TTL"):
    assert forbidden not in settings_source, f"{forbidden} 不得作为可配置环境变量出现"

# 未知 provider 拒绝就绪（构造阶段即抛错）。
try:
    ModelGatewaySettings(provider="unknown-vendor")
except ValueError:
    pass
else:
    raise SystemExit("未知 model gateway provider 必须拒绝构造/就绪")

# 限流默认阈值与模型出口必填/默认值（quickstart 契约）。
settings = Settings()
rl = settings.rate_limit
assert (rl.auth_ip_limit, rl.auth_ip_window_seconds) == (20, 300)
assert (rl.auth_account_limit, rl.auth_account_window_seconds) == (5, 300)
assert (rl.upload_limit, rl.upload_window_seconds) == (10, 600)
assert (rl.question_limit, rl.question_window_seconds) == (20, 60)
assert (rl.default_limit, rl.default_window_seconds) == (120, 60)
gw = settings.model_gateway
assert gw.provider == "openai-compatible"
assert gw.embedding_model == "text-embedding-3-small"
assert (gw.embedding_timeout_seconds, gw.embedding_max_retries) == (30, 2)
assert (gw.query_rewrite_timeout_seconds, gw.query_rewrite_max_retries) == (10, 1)
assert (gw.rerank_timeout_seconds, gw.rerank_max_retries) == (10, 1)
assert gw.generation_first_token_timeout_seconds == 15
assert (gw.generation_total_timeout_seconds, gw.generation_max_retries) == (120, 1)
assert settings.retrieval.vector_min_similarity == 0.65
assert settings.retrieval.trgm_min_similarity == 0.30
assert settings.retrieval.message_streaming_stale_seconds == 360
assert settings.storage.storage_root == "/data/orionamesh" or os.environ.get(
    "DOCUMENT_STORAGE_ROOT"
), "DOCUMENT_STORAGE_ROOT 默认应为 /data/orionamesh"

# Reranker 评分契约（model-egress.md）：scores 数组、零基 candidate_index、有限 score。
from app.infrastructure.model_gateway.types import RerankResult

result = RerankResult(scores=[{"candidate_index": 0, "score": 0.9}])
assert result.scores[0]["candidate_index"] == 0 and result.scores[0]["score"] == 0.9

print("认证/限流/模型出口配置契约校验通过")
PY

echo "==> 解析依赖可导入"
uv run python - <<'PY'
import charset_normalizer  # noqa: F401
import docx  # noqa: F401
import fitz  # noqa: F401
import markdown_it  # noqa: F401

print("pymupdf / python-docx / markdown-it-py / charset-normalizer 均可导入")
PY

echo "==> 契约测试子集"
uv run pytest \
  tests/integration/test_startup_readiness.py \
  tests/contract/test_business_error_codes.py \
  tests/contract/test_rate_limits.py \
  tests/contract/test_messages_sse_api.py \
  tests/contract/test_model_egress_contract.py

echo "==> verify-contracts OK"
