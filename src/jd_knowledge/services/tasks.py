"""提交由后台工作器执行的异步读取任务。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl

from jd_knowledge.core.ids import new_task_id
from jd_knowledge.core.models import DomainError, TaskAccepted, TaskError, TaskRecord, TaskStatus, local_now
from jd_knowledge.services.callbacks import schedule_task_callback
from jd_knowledge.services.runtime import Runtime

ReadOperation = Literal["list_workspaces", "list_files", "retrieval"]


class TaskService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def submit_read(
        self,
        *,
        operation: ReadOperation,
        user_id: str,
        workspace_id: str = "-",
        workspace_name: str = "-",
        payload: dict[str, Any] | None = None,
        callback_url: AnyHttpUrl | str | None = None,
    ) -> TaskAccepted:
        try:
            await self.runtime.tasks.health()
        except Exception as exc:
            raise DomainError("redis_unavailable", "Redis is unavailable", status_code=503) from exc

        task = TaskRecord(
            task_id=new_task_id(),
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            operation=operation,
            payload={**(payload or {}), "callback_url": str(callback_url) if callback_url is not None else None},
        )
        await self.runtime.metadata.create_task(task)
        try:
            await self.runtime.tasks.enqueue(task.task_id)
        except Exception as exc:
            error = TaskError(code="QUEUE_WRITE_FAILED", stage="submission", message="task could not be queued")
            await self.runtime.metadata.update_task(
                task.task_id,
                status=TaskStatus.FAILED,
                stage="submission",
                error=error,
                finished_at=local_now(),
            )
            task.status = TaskStatus.FAILED
            task.stage = "submission"
            task.error = error
            task.finished_at = local_now()
            schedule_task_callback(self.runtime, task)
            raise DomainError("redis_unavailable", "task could not be queued", status_code=503) from exc
        await self.runtime.task_log.write_task(
            task.task_id,
            "INFO",
            "异步查询任务已创建并进入 Redis 队列",
            operation=operation,
            user_id=user_id,
            workspace_id=workspace_id,
            callback_configured=callback_url is not None,
        )
        schedule_task_callback(self.runtime, task)
        return TaskAccepted(task_id=task.task_id)
