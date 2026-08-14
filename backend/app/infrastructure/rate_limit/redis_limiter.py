"""Redis 原子滑动窗口限流器（T028）。

- 计数、清理与判断在单次 Lua 脚本内完成，跨实例共享同一窗口（Redis 为唯一计数真相）；
- TTL 为窗口 + 清理余量；Redis 不可用时抛出 :class:`redis.RedisError`，由中间件
  按端点类别决定 fail-open 或 fail-closed；
- 响应只返回是否放行与等待秒数，不暴露内部键或成员。
"""

from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Protocol

import redis as redis_lib

from app.infrastructure.rate_limit.keys import rate_limit_key

_SCRIPT_PATH = Path(__file__).parent / "scripts" / "sliding_window.lua"


@dataclass(frozen=True)
class RateLimitDecision:
    """单条规则判定结果。"""

    allowed: bool
    retry_after_seconds: int = 0


class RateLimiter(Protocol):
    """限流器端口；中间件与测试替身共用。"""

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision: ...

    def build_key(self, policy_name: str, window_seconds: int, subject: str) -> str: ...


class RedisSlidingWindowLimiter:
    """基于 Redis 有序集合的滑动窗口限流器。"""

    def __init__(self, client: redis_lib.Redis) -> None:
        self._client = client
        self._script = self._client.register_script(_SCRIPT_PATH.read_text(encoding="utf-8"))

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        """原子检查并记录一次事件；跨实例共享计数。

        :raises redis.RedisError: Redis 不可用时向上抛出，由中间件决定降级语义。
        """
        now_ms = int(_now_ms())
        allowed, retry_after = self._script(
            keys=[key],
            args=[now_ms, window_seconds * 1000, limit],
        )
        return RateLimitDecision(allowed=bool(allowed), retry_after_seconds=int(retry_after))

    def build_key(self, policy_name: str, window_seconds: int, subject: str) -> str:
        return rate_limit_key(policy_name, window_seconds, subject)


def _now_ms() -> float:
    return time() * 1000
