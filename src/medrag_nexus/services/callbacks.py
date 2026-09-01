"""构造并投递异步任务完成回调。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from medrag_nexus.core.models import TaskRecord

if TYPE_CHECKING:
    from medrag_nexus.services.runtime import Runtime

_CALLBACK_ATTEMPTS = 3
_CALLBACK_TIMEOUT_SECONDS = 10.0


def _safe_callback_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or "unknown"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _callback_payload(task: TaskRecord) -> dict[str, Any]:
    return {
        "event": f"task.{task.status.value}",
        "task_id": task.task_id,
        "user_id": task.user_id,
        "workspace_id": task.workspace_id,
        "operation": task.operation,
        "status": task.status.value,
        "stage": task.stage,
        "progress": task.progress.model_dump(mode="json"),
        "result": task.result,
        "error": task.error.model_dump(mode="json", exclude_none=True) if task.error else None,
        "modified_at": task.modified_at.isoformat(),
    }


async def deliver_task_callback(runtime: Runtime, callback_url: str, payload: dict[str, Any]) -> None:
    safe_url = _safe_callback_url(callback_url)
    for attempt in range(1, _CALLBACK_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_CALLBACK_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    callback_url,
                    json=payload,
                    headers={
                        "X-MedRAG-Nexus-Event": str(payload["event"]),
                        "X-MedRAG-Nexus-Task-ID": str(payload["task_id"]),
                    },
                )
                response.raise_for_status()
            await runtime.task_log.write_task(
                str(payload["task_id"]),
                "INFO",
                "任务状态回调发送成功",
                callback_url=safe_url,
                callback_event=payload["event"],
                callback_status=payload["status"],
                attempt=attempt,
                response_status=response.status_code,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await runtime.task_log.write_task(
                str(payload["task_id"]),
                "WARN" if attempt < _CALLBACK_ATTEMPTS else "ERROR",
                "任务状态回调失败，将自动重试" if attempt < _CALLBACK_ATTEMPTS else "任务状态回调重试耗尽",
                callback_url=safe_url,
                callback_event=payload["event"],
                callback_status=payload["status"],
                attempt=attempt,
                max_attempts=_CALLBACK_ATTEMPTS,
                exception_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            if attempt < _CALLBACK_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))


def schedule_task_callback(runtime: Runtime, task: TaskRecord) -> None:
    callback_url = task.payload.get("callback_url")
    if not callback_url:
        return
    payload = _callback_payload(task)
    asyncio.create_task(
        deliver_task_callback(runtime, str(callback_url), payload),
        name=f"medrag-nexus-callback-{task.task_id}-{task.status.value}",
    )
