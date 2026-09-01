"""管理服务依赖、后台工作循环与应用生命周期。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import timedelta

from medrag_nexus.core.config import Settings
from medrag_nexus.core.models import local_now
from medrag_nexus.pipeline.models import EmbeddingClient, RerankClient
from medrag_nexus.services.task_log import TaskLogStore
from medrag_nexus.storage.elasticsearch import ElasticsearchStore
from medrag_nexus.storage.files import ArtifactStore
from medrag_nexus.storage.milvus import MilvusStore
from medrag_nexus.storage.redis import RedisCoordinator
from medrag_nexus.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)

# Keep BRPOP comfortably below common five-second Redis socket/proxy timeouts.
_WORKER_POLL_TIMEOUT_SECONDS = 1
_WORKER_RETRY_BASE_SECONDS = 1.0
_WORKER_RETRY_MAX_SECONDS = 15.0
_WORKER_HEARTBEAT_INTERVAL_SECONDS = 15.0


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.metadata = SQLiteStore(settings.sqlite_path)
        self.elasticsearch = ElasticsearchStore(settings)
        self.milvus = MilvusStore(settings)
        self.artifacts = ArtifactStore(settings.data_root)
        self.task_log = TaskLogStore(settings.data_root)
        self.embedding = EmbeddingClient(settings)
        self.rerank = RerankClient(settings)
        self.tasks = RedisCoordinator(settings)
        self.file_ingestion_semaphore = asyncio.Semaphore(settings.file_ingestion_concurrency)
        self.worker_tasks: list[asyncio.Task[None]] = []
        self.active_task_handles: dict[str, asyncio.Task[None]] = {}
        self.maintenance_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_log_cleanup: float = 0.0

    async def start(self, *, initialize_storage: bool = True, start_workers: bool = True) -> None:
        await self.artifacts.ensure()
        await self.task_log.ensure()
        await self.metadata.ensure()
        await self.tasks.start()
        if initialize_storage:
            await self._run_v3_migration_once()
            await self.elasticsearch.ensure_indices()
            await self.milvus.ensure_collection()
        if start_workers:
            await self._recover_queue()
            self.worker_tasks = [
                asyncio.create_task(self._worker_loop(index), name=f"medrag-nexus-worker-{index}")
                for index in range(self.settings.worker_concurrency)
            ]
            self.maintenance_task = asyncio.create_task(self._maintenance_loop(), name="medrag-nexus-maintenance")

    async def _run_v3_migration_once(self) -> None:
        if await self.metadata.has_marker(self.settings.migration_marker):
            return
        await self.elasticsearch.delete_indices(self.settings.legacy_elasticsearch_index_names)
        await self.milvus.drop_collection(self.settings.legacy_milvus_collection)
        await self.artifacts.delete_legacy_layout()
        await self.metadata.set_marker(self.settings.migration_marker)

    async def _recover_queue(self) -> None:
        for task_id in await self.metadata.queued_task_ids():
            await self.tasks.enqueue(task_id)

    async def _log_worker(self, level: str, message: str, *, worker: int, **context: object) -> None:
        """将 Worker 生命周期事件写入终端与 API 日志，日志故障不影响 Worker。"""
        try:
            await self.task_log.write_api(
                level,
                message,
                component="worker",
                worker=worker,
                **context,
            )
        except Exception:
            logger.warning("failed to write worker event log", extra={"worker": worker}, exc_info=True)

    async def close(self) -> None:
        self._stopping.set()
        if self.maintenance_task is not None:
            self.maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.maintenance_task
            self.maintenance_task = None
        workers, self.worker_tasks = self.worker_tasks, []
        if workers:
            done, pending = await asyncio.wait(workers, timeout=10)
            for worker in pending:
                worker.cancel()
            for worker in (*done, *pending):
                with suppress(asyncio.CancelledError):
                    await worker
        await self.tasks.close()
        await self.milvus.close()
        await self.elasticsearch.close()

    async def _worker_loop(self, index: int) -> None:
        from medrag_nexus.services.processing import process_task

        # 测试替身及旧式手工构造的 Runtime 也能安全进入 Worker 循环。
        if not hasattr(self, "active_task_handles"):
            self.active_task_handles = {}

        # 独立心跳协程：与任务处理解耦，任务再久心跳也不会断
        async def beat() -> None:
            failed_attempts = 0
            while not self._stopping.is_set():
                try:
                    await self.tasks.heartbeat()
                    if failed_attempts:
                        await self._log_worker(
                            "INFO",
                            "Worker Redis 心跳连接已恢复",
                            worker=index,
                            failed_attempts=failed_attempts,
                        )
                    failed_attempts = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_attempts += 1
                    logger.warning("worker heartbeat failed", extra={"worker": index}, exc_info=True)
                    await self._log_worker(
                        "ERROR",
                        "Worker Redis 心跳失败，将继续重试",
                        worker=index,
                        failed_attempts=failed_attempts,
                        exception_type=type(exc).__name__,
                        error=str(exc)[:500],
                        retry_delay_seconds=_WORKER_HEARTBEAT_INTERVAL_SECONDS,
                    )
                await asyncio.sleep(_WORKER_HEARTBEAT_INTERVAL_SECONDS)

        heartbeat_task = asyncio.create_task(beat(), name=f"medrag-nexus-heartbeat-{index}")
        dequeue_failures = 0
        await self._log_worker(
            "INFO",
            "Worker 已启动",
            worker=index,
            queue=self.settings.redis_queue_name,
            poll_timeout_seconds=_WORKER_POLL_TIMEOUT_SECONDS,
        )
        try:
            while not self._stopping.is_set():
                try:
                    task_id = await self.tasks.dequeue(timeout=_WORKER_POLL_TIMEOUT_SECONDS)
                    if dequeue_failures:
                        await self._log_worker(
                            "INFO",
                            "Worker Redis 队列连接已恢复",
                            worker=index,
                            queue=self.settings.redis_queue_name,
                            failed_attempts=dequeue_failures,
                        )
                    dequeue_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    dequeue_failures += 1
                    retry_delay = min(
                        _WORKER_RETRY_BASE_SECONDS * (2 ** min(dequeue_failures - 1, 4)),
                        _WORKER_RETRY_MAX_SECONDS,
                    )
                    logger.warning("worker Redis dequeue failed", extra={"worker": index}, exc_info=True)
                    await self._log_worker(
                        "ERROR",
                        "Worker Redis 取队列失败，将自动重试",
                        worker=index,
                        queue=self.settings.redis_queue_name,
                        failed_attempts=dequeue_failures,
                        exception_type=type(exc).__name__,
                        error=str(exc)[:500],
                        retry_delay_seconds=retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                if task_id is None:
                    continue
                await self._log_worker(
                    "INFO",
                    "Worker 已从 Redis 队列取得任务",
                    worker=index,
                    queue=self.settings.redis_queue_name,
                    task_id=task_id,
                )
                job = asyncio.create_task(process_task(self, task_id), name=f"knowledge-task-{task_id}")
                self.active_task_handles[task_id] = job
                try:
                    await job
                except asyncio.CancelledError:
                    if self._stopping.is_set():
                        if not job.done():
                            job.cancel()
                        raise
                    # 用户主动取消业务任务时，Worker 本身继续处理后续队列。
                    continue
                except Exception:
                    logger.exception("worker failed outside task boundary", extra={"task_id": task_id, "worker": index})
                    await self.task_log.write_task(
                        task_id,
                        "ERROR",
                        "worker 处理任务时发生未捕获异常",
                        worker=index,
                    )
                finally:
                    self.active_task_handles.pop(task_id, None)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self._log_worker(
                "INFO",
                "Worker 已停止",
                worker=index,
                queue=self.settings.redis_queue_name,
                stopping=self._stopping.is_set(),
            )

    async def cancel_active_task(self, task_id: str) -> bool:
        """取消当前进程正在执行的业务任务，并等待其补偿清理完成。"""

        task = self.active_task_handles.get(task_id)
        if task is None or task.done():
            return False
        task.cancel("user_cancelled")
        with suppress(asyncio.CancelledError):
            await task
        return True

    async def _maintenance_loop(self) -> None:
        from medrag_nexus.services.processing import recover_interrupted_task

        while not self._stopping.is_set():
            await asyncio.sleep(60)
            try:
                cutoff = local_now() - timedelta(seconds=self.settings.workspace_lock_ttl_seconds * 2)
                for task in await self.metadata.stale_running_tasks(cutoff):
                    if not await self.tasks.workspace_locked(task.user_id, task.workspace_id):
                        await recover_interrupted_task(self, task)
                await self.metadata.cleanup_tasks(self.settings.task_retention_days)
                await self.artifacts.cleanup_stale_staging()
                now_monotonic = time.monotonic()
                if now_monotonic - self._last_log_cleanup >= 86400:
                    await self.task_log.cleanup(self.settings.task_log_retention_days)
                    self._last_log_cleanup = now_monotonic
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance cycle failed")
