"""Redis 原子滑动窗口限流器集成测试（T030）。

覆盖：原子计数与 TTL 清理、窗口过期后恢复、跨实例共享计数、并发临界值不放行
超限请求。Redis 不可用时跳过（本地/CI 需提供 Redis）。
"""

import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis as redis_lib

from app.core.redis import get_redis_client
from app.infrastructure.rate_limit.redis_limiter import RedisSlidingWindowLimiter


@pytest.fixture(scope="module")
def redis_client() -> Generator[redis_lib.Redis, None, None]:
    client = get_redis_client()
    try:
        client.ping()
    except redis_lib.RedisError as exc:
        pytest.skip(f"redis unavailable: {exc}")
    yield client
    for key in client.scan_iter("rl:*"):
        client.delete(key)


@pytest.fixture()
def limiter(redis_client: redis_lib.Redis) -> Generator[RedisSlidingWindowLimiter, None, None]:
    yield RedisSlidingWindowLimiter(redis_client)
    for key in redis_client.scan_iter("rl:*"):
        redis_client.delete(key)


class TestSlidingWindow:
    def test_allows_until_limit(self, limiter: RedisSlidingWindowLimiter) -> None:
        key = limiter.build_key("test-policy", 60, "subject-a")
        for _ in range(5):
            decision = limiter.check(key, 5, 60)
            assert decision.allowed
        denied = limiter.check(key, 5, 60)
        assert not denied.allowed
        assert denied.retry_after_seconds >= 1

    def test_retry_after_is_positive_and_bounded(self, limiter: RedisSlidingWindowLimiter) -> None:
        key = limiter.build_key("test-policy", 60, "subject-b")
        for _ in range(3):
            limiter.check(key, 3, 60)
        denied = limiter.check(key, 3, 60)
        assert denied.retry_after_seconds >= 1
        assert denied.retry_after_seconds <= 60

    def test_window_expiry_resets_count(self, limiter: RedisSlidingWindowLimiter) -> None:
        key = limiter.build_key("test-policy", 1, "subject-c")
        for _ in range(2):
            assert limiter.check(key, 2, 1).allowed
        assert not limiter.check(key, 2, 1).allowed
        time.sleep(1.2)
        assert limiter.check(key, 2, 1).allowed

    def test_distinct_subjects_have_independent_counts(
        self, limiter: RedisSlidingWindowLimiter
    ) -> None:
        key_a = limiter.build_key("test-policy", 60, "subject-d")
        key_b = limiter.build_key("test-policy", 60, "subject-e")
        for _ in range(2):
            limiter.check(key_a, 2, 60)
        assert not limiter.check(key_a, 2, 60).allowed
        assert limiter.check(key_b, 2, 60).allowed


class TestCrossInstance:
    def test_shared_counting_across_instances(self, redis_client: redis_lib.Redis) -> None:
        limiter_a = RedisSlidingWindowLimiter(redis_client)
        limiter_b = RedisSlidingWindowLimiter(redis_client)
        key = limiter_a.build_key("test-policy", 60, "shared-subject")
        assert limiter_a.check(key, 3, 60).allowed
        assert limiter_b.check(key, 3, 60).allowed
        assert limiter_a.check(key, 3, 60).allowed
        # 两个实例共享同一窗口：第四次拒绝。
        assert not limiter_b.check(key, 3, 60).allowed


class TestConcurrency:
    def test_no_over_admission_at_limit(self, redis_client: redis_lib.Redis) -> None:
        limiter = RedisSlidingWindowLimiter(redis_client)
        key = limiter.build_key("test-policy", 60, "concurrent-subject")
        limit = 5
        total = limit + 10
        allowed = 0

        def _attempt() -> bool:
            return limiter.check(key, limit, 60).allowed

        with ThreadPoolExecutor(max_workers=total) as pool:
            results = list(pool.map(lambda _: _attempt(), range(total)))
        allowed = sum(1 for r in results if r)
        assert allowed == limit
