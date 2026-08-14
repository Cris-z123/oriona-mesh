"""Redis 连接与队列配置（T016）。

Redis 仅用于瞬时限流计数与 Celery 队列传输；任务状态、租户边界等业务真相只以
PostgreSQL 为准（Redis/Celery 不得作为业务状态存储或读取真相源）。
连接串来自唯一根配置模块；客户端惰性创建，进程启动不依赖 Redis 在线。
"""

import redis as redis_lib

from app.core.settings import get_settings

# 默认任务队列名；Celery 与维护扫描器共用。
TASK_QUEUE_NAME = "orionamesh.tasks"

_READY_TIMEOUT_SECONDS = 2.0


def get_redis_client(settings=None) -> redis_lib.Redis:
    """创建 Redis 客户端（惰性连接，短超时便于健康检查与 fail-closed 降级）。"""
    settings = settings or get_settings()
    return redis_lib.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=_READY_TIMEOUT_SECONDS,
        socket_timeout=_READY_TIMEOUT_SECONDS,
    )


def redis_healthy(client: redis_lib.Redis | None = None) -> bool:
    """PING 健康检查；Redis 不可用时返回 False（不抛出）。"""
    client = client or get_redis_client()
    try:
        return bool(client.ping())
    except redis_lib.RedisError:
        return False


def broker_url() -> str:
    """Celery broker 地址；与限流共享同一 Redis 连接串。"""
    return get_settings().redis_url
