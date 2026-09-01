"""验证 Redis 队列空闲超时和健康异常处理。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from jd_knowledge.storage.redis import RedisCoordinator


async def test_dequeue_treats_idle_read_timeout_as_empty_when_redis_is_healthy() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.ping_calls = 0
            self.deleted: list[str] = []

        async def brpop(self, queue: str, *, timeout: int):
            assert queue == "knowledge:test"
            assert timeout == 1
            raise RedisTimeoutError("Timeout reading from Redis")

        async def ping(self) -> bool:
            self.ping_calls += 1
            return True

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    coordinator = object.__new__(RedisCoordinator)
    coordinator.settings = SimpleNamespace(redis_queue_name="knowledge:test")
    coordinator.redis = FakeRedis()

    assert await coordinator.dequeue(timeout=1) is None
    assert coordinator.redis.ping_calls == 1
    assert coordinator.redis.deleted == []


async def test_dequeue_propagates_timeout_when_redis_health_check_fails() -> None:
    class FakeRedis:
        async def brpop(self, queue: str, *, timeout: int):
            raise RedisTimeoutError("Timeout reading from Redis")

        async def ping(self) -> bool:
            raise ConnectionError("Redis unavailable")

    coordinator = object.__new__(RedisCoordinator)
    coordinator.settings = SimpleNamespace(redis_queue_name="knowledge:test")
    coordinator.redis = FakeRedis()

    with pytest.raises(ConnectionError, match="Redis unavailable"):
        await coordinator.dequeue(timeout=1)
