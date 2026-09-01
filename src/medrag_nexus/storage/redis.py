"""通过 Redis 协调任务队列、租约和并发写入。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import monotonic
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from medrag_nexus.core.config import Settings
from medrag_nexus.core.models import FileBusyError

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisCoordinator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def start(self) -> None:
        await self.redis.ping()

    async def close(self) -> None:
        await self.redis.aclose()

    async def health(self) -> None:
        await self.redis.ping()

    async def heartbeat(self, ttl: int = 45) -> None:
        await self.redis.set("knowledge:worker:heartbeat", "ok", ex=ttl)

    async def worker_alive(self) -> bool:
        return bool(await self.redis.exists("knowledge:worker:heartbeat"))

    async def enqueue(self, task_id: str) -> None:
        marker = f"knowledge:queued:{task_id}"
        inserted = await self.redis.set(marker, "1", nx=True, ex=self.settings.task_timeout_seconds + 3600)
        if not inserted:
            return
        try:
            await self.redis.lpush(self.settings.redis_queue_name, task_id)
        except BaseException:
            await self.redis.delete(marker)
            raise

    async def dequeue(self, timeout: int = 5) -> str | None:
        try:
            result = await self.redis.brpop(self.settings.redis_queue_name, timeout=timeout)
        except RedisTimeoutError:
            # A blocking pop can reach the client's socket timeout while Redis is
            # healthy and the queue is simply empty. Verify the connection before
            # treating that expected idle timeout as a Redis outage.
            await self.redis.ping()
            return None
        if result is None:
            return None
        task_id = str(result[1])
        await self.redis.delete(f"knowledge:queued:{task_id}")
        return task_id

    async def cancel_queued(self, task_id: str) -> bool:
        """从等待队列移除任务；任务已被 Worker 取走时返回 False。"""

        removed = await self.redis.lrem(self.settings.redis_queue_name, 0, task_id)
        await self.redis.delete(f"knowledge:queued:{task_id}")
        return bool(removed)

    @staticmethod
    def workspace_lock_key(user_id: str, workspace_id: str) -> str:
        return f"knowledge:{user_id}:{workspace_id}:lock"

    async def workspace_locked(self, user_id: str, workspace_id: str) -> bool:
        return bool(await self.redis.exists(self.workspace_lock_key(user_id, workspace_id)))

    @asynccontextmanager
    async def workspace_lock(
        self,
        user_id: str,
        workspace_id: str,
        *,
        wait_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        key = self.workspace_lock_key(user_id, workspace_id)
        token = uuid4().hex
        wait = wait_seconds if wait_seconds is not None else self.settings.workspace_lock_wait_seconds
        deadline = monotonic() + wait
        while True:
            acquired = await self.redis.set(key, token, nx=True, ex=self.settings.workspace_lock_ttl_seconds)
            if acquired:
                break
            if monotonic() >= deadline:
                active = await self.redis.get(key)
                raise FileBusyError(active or "unknown")
            await asyncio.sleep(0.1)

        lost = asyncio.Event()

        async def renew() -> None:
            interval = max(1, self.settings.workspace_lock_ttl_seconds // 3)
            while True:
                await asyncio.sleep(interval)
                renewed = await self.redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    token,
                    self.settings.workspace_lock_ttl_seconds,
                )
                if not renewed:
                    lost.set()
                    return

        heartbeat = asyncio.create_task(renew(), name=f"workspace-lock-{workspace_id}")
        try:
            yield
            if lost.is_set():
                raise RuntimeError("workspace lock ownership was lost")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.redis.eval(_RELEASE_SCRIPT, 1, key, token)

    def _content_key(self, user_id: str, workspace_id: str, source_type: str, digest: str) -> str:
        return f"knowledge:content:{user_id}:{workspace_id}:{source_type}:{digest}"

    async def reserve_content(
        self,
        user_id: str,
        workspace_id: str,
        source_type: str,
        digest: str,
        task_id: str,
    ) -> str | None:
        key = self._content_key(user_id, workspace_id, source_type, digest)
        acquired = await self.redis.set(key, task_id, nx=True, ex=self.settings.reservation_ttl_seconds)
        return None if acquired else await self.redis.get(key)

    async def release_content(
        self,
        user_id: str,
        workspace_id: str,
        source_type: str,
        digest: str,
        task_id: str,
    ) -> None:
        key = self._content_key(user_id, workspace_id, source_type, digest)
        await self.redis.eval(_RELEASE_SCRIPT, 1, key, task_id)

    def _file_key(self, user_id: str, workspace_id: str, file_id: str) -> str:
        return f"knowledge:file:{user_id}:{workspace_id}:{file_id}"

    async def reserve_file(self, user_id: str, workspace_id: str, file_id: str, task_id: str) -> None:
        key = self._file_key(user_id, workspace_id, file_id)
        acquired = await self.redis.set(key, task_id, nx=True, ex=self.settings.reservation_ttl_seconds)
        if not acquired:
            raise FileBusyError(await self.redis.get(key) or "unknown")

    async def release_file(self, user_id: str, workspace_id: str, file_id: str, task_id: str) -> None:
        key = self._file_key(user_id, workspace_id, file_id)
        await self.redis.eval(_RELEASE_SCRIPT, 1, key, task_id)
