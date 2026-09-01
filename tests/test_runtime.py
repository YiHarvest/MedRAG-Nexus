"""验证后台工作器重试、心跳和维护循环。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from medrag_nexus.services import runtime as runtime_module
from medrag_nexus.services.runtime import Runtime


class CapturingTaskLog:
    def __init__(self) -> None:
        self.api_events: list[tuple[str, str, dict[str, object]]] = []

    async def write_api(self, level: str, message: str, **context: object) -> None:
        self.api_events.append((level, message, context))


async def test_worker_retries_dequeue_and_logs_redis_recovery(monkeypatch) -> None:
    task_id = "a" * 32
    processed: list[str] = []

    class FakeTasks:
        def __init__(self) -> None:
            self.dequeue_calls = 0

        async def heartbeat(self) -> None:
            return None

        async def dequeue(self, *, timeout: int) -> str | None:
            assert timeout == runtime_module._WORKER_POLL_TIMEOUT_SECONDS
            self.dequeue_calls += 1
            if self.dequeue_calls == 1:
                raise ConnectionError("temporary Redis disconnect")
            return task_id

    runtime = object.__new__(Runtime)
    runtime.settings = SimpleNamespace(redis_queue_name="knowledge:test")
    runtime.tasks = FakeTasks()
    runtime.task_log = CapturingTaskLog()
    runtime._stopping = asyncio.Event()

    async def fake_process_task(selected_runtime: Runtime, selected_task_id: str) -> None:
        assert selected_runtime is runtime
        processed.append(selected_task_id)
        runtime._stopping.set()

    monkeypatch.setattr("medrag_nexus.services.processing.process_task", fake_process_task)
    monkeypatch.setattr(runtime_module, "_WORKER_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(runtime_module, "_WORKER_RETRY_MAX_SECONDS", 0.0)

    await asyncio.wait_for(runtime._worker_loop(2), timeout=1)

    assert processed == [task_id]
    assert runtime.tasks.dequeue_calls == 2
    messages = [message for _, message, _ in runtime.task_log.api_events]
    assert messages == [
        "Worker 已启动",
        "Worker Redis 取队列失败，将自动重试",
        "Worker Redis 队列连接已恢复",
        "Worker 已从 Redis 队列取得任务",
        "Worker 已停止",
    ]
    failure = runtime.task_log.api_events[1]
    assert failure[0] == "ERROR"
    assert failure[2]["exception_type"] == "ConnectionError"
    assert failure[2]["failed_attempts"] == 1
    assert failure[2]["retry_delay_seconds"] == 0.0


async def test_maintenance_does_not_write_worker_heartbeat(monkeypatch) -> None:
    class FakeTasks:
        def __init__(self) -> None:
            self.heartbeat_calls = 0

        async def heartbeat(self) -> None:
            self.heartbeat_calls += 1

    class FakeMetadata:
        async def stale_running_tasks(self, cutoff):
            return []

        async def cleanup_tasks(self, retention_days: int) -> None:
            assert retention_days == 30

    class FakeArtifacts:
        async def cleanup_stale_staging(self) -> None:
            return None

    runtime = object.__new__(Runtime)
    runtime.settings = SimpleNamespace(
        workspace_lock_ttl_seconds=60,
        task_retention_days=30,
        task_log_retention_days=7,
    )
    runtime.tasks = FakeTasks()
    runtime.metadata = FakeMetadata()
    runtime.artifacts = FakeArtifacts()
    runtime.task_log = CapturingTaskLog()
    runtime._stopping = asyncio.Event()
    runtime._last_log_cleanup = time.monotonic()

    async def finish_first_sleep(delay: float) -> None:
        assert delay == 60
        runtime._stopping.set()

    monkeypatch.setattr(runtime_module.asyncio, "sleep", finish_first_sleep)

    await runtime._maintenance_loop()

    assert runtime.tasks.heartbeat_calls == 0
