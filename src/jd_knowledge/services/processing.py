"""处理异步知识任务，并协调解析、索引、补偿和恢复流程。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from jd_knowledge.core.ids import chunk_id, content_hash
from jd_knowledge.core.models import (
    ChunkRecord,
    DomainError,
    ResourceRecord,
    RetrievalRequest,
    TaskError,
    TaskProgress,
    TaskRecord,
    TaskStatus,
    WorkspaceRecord,
    local_now,
)
from jd_knowledge.pipeline.markdown import chunk_markdown, normalize_markdown
from jd_knowledge.pipeline.parsers import ParseResult, parse_file
from jd_knowledge.services.callbacks import schedule_task_callback
from jd_knowledge.services.runtime import Runtime

T = TypeVar("T")
_PARSING_PROGRESS_INTERVAL_SECONDS = 5.0
_FILE_SLOT_WAIT_LOG_INTERVAL_SECONDS = 5.0


class StageFailure(Exception):
    def __init__(self, code: str, stage: str, attempts: int, cause: BaseException):
        super().__init__(str(cause))
        self.code = code
        self.stage = stage
        self.attempts = attempts
        self.cause = cause


async def _progress(runtime: Runtime, task: TaskRecord, stage: str, current: int, total: int = 100) -> None:
    percent = round(current / total * 100, 2)
    progress = TaskProgress(current=current, total=total, percent=percent)
    await runtime.metadata.update_task(
        task.task_id,
        status=TaskStatus.RUNNING,
        stage=stage,
        progress=progress,
    )
    task.status = TaskStatus.RUNNING
    task.stage = stage
    task.progress = progress
    task.modified_at = local_now()
    await runtime.task_log.write_task(
        task.task_id,
        "INFO",
        "任务进度更新",
        stage=stage,
        current=current,
        total=total,
        progress=f"{percent}%",
    )
    schedule_task_callback(runtime, task)


async def _retry(
    runtime: Runtime,
    task: TaskRecord,
    *,
    stage: str,
    current: int,
    code: str,
    operation: Callable[[], Awaitable[T]],
) -> T:
    last: BaseException | None = None
    for attempt in range(1, 5):
        await _progress(runtime, task, stage, current)
        started = time.monotonic()
        await runtime.task_log.write_task(
            task.task_id,
            "INFO",
            "处理阶段开始",
            operation=task.operation,
            stage=stage,
            attempt=attempt,
            max_attempts=4,
        )
        try:
            result = await operation()
            await runtime.task_log.write_task(
                task.task_id,
                "INFO",
                "处理阶段完成",
                operation=task.operation,
                stage=stage,
                attempt=attempt,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            last = exc
            retry_delay = 2 ** (attempt - 1) if attempt < 4 else 0
            await runtime.task_log.write_task(
                task.task_id,
                "WARN" if attempt < 4 else "ERROR",
                "处理阶段失败，等待重试" if attempt < 4 else "处理阶段重试耗尽",
                operation=task.operation,
                stage=stage,
                attempt=attempt,
                max_attempts=4,
                exception_type=type(exc).__name__,
                error=str(exc)[:500],
                retry_delay_seconds=retry_delay,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            if attempt < 4:
                await asyncio.sleep(retry_delay)
    assert last is not None
    raise StageFailure(code, stage, 4, last)


async def _parse_file_with_progress(
    runtime: Runtime,
    task: TaskRecord,
    source_path: Path,
    file_name: str,
) -> ParseResult:
    parse_percent = 10
    await _progress(runtime, task, "parsing", parse_percent)

    async def advance(increment: int = 1) -> None:
        nonlocal parse_percent
        next_percent = min(44, parse_percent + increment)
        if next_percent <= parse_percent:
            return
        parse_percent = next_percent
        await _progress(runtime, task, "parsing", parse_percent)

    async def report(level: str, message: str, context: dict[str, object]) -> None:
        await runtime.task_log.write_task(
            task.task_id,
            level,
            message,
            stage="parsing",
            file_name=file_name,
            **context,
        )
        await advance(5)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_PARSING_PROGRESS_INTERVAL_SECONDS)
            await advance()

    heartbeat_task = asyncio.create_task(
        heartbeat(),
        name=f"jd-knowledge-parsing-progress-{task.task_id}",
    )
    try:
        return await parse_file(source_path, runtime.settings, progress=report)
    except Exception as exc:
        raise StageFailure("FILE_PARSE_FAILED", "parsing", 1, exc) from exc
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _acquire_file_ingestion_slot(runtime: Runtime, task: TaskRecord) -> None:
    waited_seconds = 0.0
    await _progress(runtime, task, "waiting_for_parser", 5)
    await runtime.task_log.write_task(
        task.task_id,
        "INFO",
        "大文件任务正在等待解析槽位",
        stage="waiting_for_parser",
        concurrency_limit=runtime.settings.file_ingestion_concurrency,
    )
    while True:
        try:
            await asyncio.wait_for(
                runtime.file_ingestion_semaphore.acquire(),
                timeout=_FILE_SLOT_WAIT_LOG_INTERVAL_SECONDS,
            )
            await runtime.task_log.write_task(
                task.task_id,
                "INFO",
                "大文件任务已取得解析槽位",
                stage="waiting_for_parser",
                waited_seconds=round(waited_seconds, 1),
                concurrency_limit=runtime.settings.file_ingestion_concurrency,
            )
            return
        except TimeoutError:
            waited_seconds += _FILE_SLOT_WAIT_LOG_INTERVAL_SECONDS
            await _progress(runtime, task, "waiting_for_parser", 5)
            await runtime.task_log.write_task(
                task.task_id,
                "INFO",
                "大文件任务继续等待解析槽位",
                stage="waiting_for_parser",
                waited_seconds=round(waited_seconds, 1),
                concurrency_limit=runtime.settings.file_ingestion_concurrency,
            )


async def _complete(
    runtime: Runtime,
    task: TaskRecord,
    result: dict[str, Any],
    *,
    persist: bool = True,
    completed_at: datetime | None = None,
) -> None:
    now = completed_at or local_now()
    progress = TaskProgress(current=100, total=100, percent=100.0)
    if persist:
        await runtime.metadata.update_task(
            task.task_id,
            status=TaskStatus.SUCCEEDED,
            stage="completed",
            progress=progress,
            result=result,
            error=None,
            finished_at=now,
        )
    task.status = TaskStatus.SUCCEEDED
    task.stage = "completed"
    task.progress = progress
    task.result = result
    task.error = None
    task.finished_at = now
    task.modified_at = now
    try:
        await runtime.task_log.write_task(
            task.task_id,
            "INFO",
            "任务完成",
            operation=task.operation,
            user_id=task.user_id,
            workspace_id=task.workspace_id,
        )
    except Exception:
        pass
    schedule_task_callback(runtime, task)


async def _journal(runtime: Runtime, task: TaskRecord, **values: Any) -> None:
    task.journal.update(values)
    await runtime.metadata.update_task(task.task_id, journal=task.journal)


def _task_error(exc: BaseException) -> TaskError:
    if isinstance(exc, StageFailure):
        return TaskError(
            code=exc.code,
            stage=exc.stage,
            message=str(exc.cause)[:2000],
            attempts=exc.attempts,
        )
    if isinstance(exc, DomainError):
        return TaskError(code=exc.code.upper(), stage="parsing", message=exc.message[:2000], attempts=1)
    if isinstance(exc, asyncio.CancelledError):
        if exc.args and exc.args[0] == "user_cancelled":
            return TaskError(
                code="TASK_CANCELLED",
                stage="cancelled",
                message="task was cancelled by the user",
            )
        return TaskError(code="PROCESS_INTERRUPTED", stage="interrupted", message="worker process was interrupted")
    return TaskError(code="TASK_FAILED", stage="unknown", message=str(exc)[:2000], attempts=1)


async def _fail(
    runtime: Runtime,
    task: TaskRecord,
    error: TaskError,
    compensation_errors: list[str],
) -> None:
    if compensation_errors:
        error.requires_repair = True
        error.compensation_error = "; ".join(compensation_errors)[:2000]
        await runtime.metadata.block_workspace(task.workspace_id, error.compensation_error)
    now = local_now()
    await runtime.metadata.update_task(
        task.task_id,
        status=TaskStatus.FAILED,
        stage=error.stage,
        error=error,
        finished_at=now,
    )
    task.status = TaskStatus.FAILED
    task.stage = error.stage
    task.error = error
    task.finished_at = now
    task.modified_at = now
    await runtime.task_log.write_task(
        task.task_id,
        "ERROR",
        f"任务失败: {error.code} - {error.message}",
        operation=task.operation,
        stage=error.stage,
        requires_repair=error.requires_repair,
    )
    schedule_task_callback(runtime, task)


def _title(markdown: str, file_name: str | None, source_type: str) -> str:
    if source_type == "str":
        return "Workspace string knowledge"
    for line in markdown.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith("<!--"):
            return stripped[:200]
    return Path(file_name).stem if file_name else "Document"


def _build_chunks(runtime: Runtime, task: TaskRecord, resource: ResourceRecord, markdown: str) -> list[ChunkRecord]:
    title = _title(markdown, resource.file_name, resource.source_type)
    spans = chunk_markdown(
        markdown,
        chunk_size=runtime.settings.chunk_size,
        chunk_overlap=runtime.settings.chunk_overlap,
    )
    return [
        ChunkRecord(
            chunk_id=chunk_id(resource.document_id, span.ordinal, span.content),
            workspace_id=task.workspace_id,
            user_id=task.user_id,
            document_id=resource.document_id,
            source_type=resource.source_type,
            file_id=resource.file_id,
            file_name=resource.file_name,
            ordinal=span.ordinal,
            content=span.content,
            content_hash=content_hash(span.content.encode("utf-8")),
            section=span.section if resource.source_type == "file" and span.section else None,
            page_number=span.page_number if resource.source_type == "file" else None,
            chunk_type=span.chunk_type,  # type: ignore[arg-type]
            start_offset=span.start_offset,
            end_offset=span.end_offset,
            embedding_text=(
                f"文档标题：{title}\n章节标题：{span.section}\n正文：{span.content}"
                if resource.source_type == "file"
                else span.content
            ),
        )
        for span in spans
    ]


def _project_workspace(
    current: WorkspaceRecord | None,
    task: TaskRecord,
    *,
    source_type: str,
    size_delta: int,
    count_delta: int,
) -> WorkspaceRecord:
    now = local_now()
    base = current or WorkspaceRecord(
        workspace_id=task.workspace_id,
        user_id=task.user_id,
        workspace_name=task.workspace_name,
        created_at=now,
        modified_at=now,
    )
    return base.model_copy(
        update={
            "resource_count": base.resource_count + count_delta,
            "file_count": base.file_count + (count_delta if source_type == "file" else 0),
            "str_count": base.str_count + (count_delta if source_type == "str" else 0),
            "total_size_bytes": base.total_size_bytes + size_delta,
            "modified_at": now,
        }
    )


async def _cleanup_index(runtime: Runtime, resource: ResourceRecord) -> None:
    await asyncio.gather(
        runtime.elasticsearch.delete_resource(resource.workspace_id, resource.document_id),
        runtime.milvus.delete_resource(resource.workspace_id, resource.document_id),
    )


async def _compensate_add(runtime: Runtime, task: TaskRecord, resource: ResourceRecord | None) -> list[str]:
    errors: list[str] = []
    await _progress(runtime, task, "compensating", 80)
    if resource is not None:
        artifact_operation = (
            (lambda: runtime.artifacts.delete_artifact(resource.artifact_path))
            if resource.source_type == "file"
            else (
                lambda: runtime.artifacts.remove_string_record(
                    task.user_id,
                    task.workspace_id,
                    resource.content_hash,
                )
            )
        )
        for label, operation in (
            ("index", lambda: _cleanup_index(runtime, resource)),
            ("metadata", lambda: _delete_metadata_if_present(runtime, resource)),
            ("artifact", artifact_operation),
        ):
            try:
                await operation()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
    try:
        await runtime.artifacts.cleanup_staging(task.task_id)
    except Exception as exc:
        errors.append(f"staging: {exc}")
    return errors


async def _delete_metadata_if_present(runtime: Runtime, resource: ResourceRecord) -> None:
    existing = await runtime.metadata.get_resource_by_document(resource.workspace_id, resource.document_id)
    if existing is not None:
        await runtime.metadata.delete_resource(existing)
    workspace = await runtime.metadata.get_workspace(resource.workspace_id)
    if workspace is not None:
        await runtime.elasticsearch.mirror_workspace(workspace)
    else:
        await runtime.elasticsearch.delete_workspace(resource.workspace_id)


async def _publish_then_record_resource(
    runtime: Runtime,
    task: TaskRecord,
    resource: ResourceRecord,
    *,
    source_type: str,
    prepared: Path,
    target: Path,
    digest: str,
    result: dict[str, Any],
) -> None:
    """Publish the durable artifact before making the resource visible in SQLite lists."""

    async def finalize() -> None:
        await runtime.task_log.write_task(
            task.task_id,
            "INFO",
            "开始原子保存字符串知识到 Workspace JSONL"
            if source_type == "str"
            else "开始原子发布文件与 Markdown 产物",
            stage="finalizing",
            target_path=target,
            size_bytes=resource.size_bytes,
            content_hash=digest,
        )
        await asyncio.to_thread(runtime.artifacts.publish_file, prepared, target)
        await runtime.task_log.write_task(
            task.task_id,
            "INFO",
            "字符串知识已成功保存到 Workspace JSONL"
            if source_type == "str"
            else "文件与 Markdown 产物已成功发布",
            stage="finalizing",
            target_path=target,
            size_bytes=resource.size_bytes,
            content_hash=digest,
        )

    await _retry(
        runtime,
        task,
        stage="finalizing",
        current=75,
        code="FILE_FINALIZE_FAILED",
        operation=finalize,
    )
    await _journal(runtime, task, artifact_finalized=True)
    await runtime.artifacts.cleanup_staging(task.task_id)
    completed_at = local_now()

    async def write_metadata() -> int:
        return await runtime.metadata.add_resource_and_complete_task(resource, task, result, completed_at)

    row_id = await _retry(
        runtime,
        task,
        stage="metadata",
        current=90,
        code="DATABASE_STATS_UPDATE_FAILED",
        operation=write_metadata,
    )
    resource.row_id = row_id
    task.journal.update(metadata_written=True, resource=resource.model_dump(mode="json"))
    await _complete(runtime, task, result, persist=False, completed_at=completed_at)


async def _process_add(runtime: Runtime, task: TaskRecord) -> None:
    payload = task.payload
    source_type = str(payload["source_type"])
    digest = str(payload["content_hash"])
    resource: ResourceRecord | None = None
    cancelled = False
    file_slot_acquired = False
    try:
        await runtime.metadata.update_task(task.task_id, started_at=local_now(), status=TaskStatus.RUNNING)
        if source_type == "file":
            await _acquire_file_ingestion_slot(runtime, task)
            file_slot_acquired = True
        async with runtime.tasks.workspace_lock(task.user_id, task.workspace_id):
            duplicate = await runtime.metadata.find_duplicate(task.workspace_id, source_type, digest)
            if duplicate is not None:
                raise DomainError("duplicate_content", "the same content already exists", status_code=409)

            source_path = Path(str(payload["source_path"]))
            if source_type == "file":
                file_name = str(payload.get("file_name") or source_path.name)
                parsed = await _parse_file_with_progress(runtime, task, source_path, file_name)
                indexed_text = normalize_markdown(parsed.markdown)
                parser = parsed.parser
                degraded = parsed.degraded
            else:
                await _progress(runtime, task, "parsing", 10)
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "开始读取并规范化字符串知识",
                    stage="parsing",
                    size_bytes=payload["size_bytes"],
                )
                indexed_text = normalize_markdown(await asyncio.to_thread(source_path.read_text, encoding="utf-8"))
                parser = "text"
                degraded = False
            await _progress(runtime, task, "parsing_complete", 45)
            if not indexed_text:
                raise StageFailure("EMPTY_DOCUMENT", "parsing", 1, ValueError("resource contains no usable text"))
            await runtime.task_log.write_task(
                task.task_id,
                "WARN" if degraded else "INFO",
                "文档解析与 Markdown 规范化完成",
                stage="parsing",
                parser=parser,
                degraded=degraded,
                markdown_chars=len(indexed_text),
            )

            now = local_now()
            document_id = UUID(str(payload["document_id"]))
            if source_type == "file":
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "开始准备文件与 Markdown 产物",
                    stage="preparing_artifact",
                    file_name=payload["file_name"],
                )
                prepared, target = await runtime.artifacts.prepare_file(
                    task_id=task.task_id,
                    user_id=task.user_id,
                    workspace_id=task.workspace_id,
                    file_id=str(payload["file_id"]),
                    file_name=str(payload["file_name"]),
                    source_path=source_path,
                    markdown=indexed_text,
                )
                artifact_path = str(target)
            else:
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "开始准备字符串知识 JSONL 记录",
                    stage="preparing_artifact",
                    size_bytes=payload["size_bytes"],
                    content_hash=digest,
                )
                original = await asyncio.to_thread(source_path.read_text, encoding="utf-8")
                record = {
                    "content": original,
                    "content_hash": digest,
                    "size_bytes": int(payload["size_bytes"]),
                    "created_at": now.isoformat(),
                    "modified_at": now.isoformat(),
                }
                prepared, target = await runtime.artifacts.prepare_string_record(
                    task_id=task.task_id,
                    user_id=task.user_id,
                    workspace_id=task.workspace_id,
                    record=record,
                )
                artifact_path = str(target)
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "字符串知识 JSONL 临时记录准备完成",
                    stage="preparing_artifact",
                    target_path=artifact_path,
                    size_bytes=payload["size_bytes"],
                    content_hash=digest,
                )
            if source_type == "file":
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "文件与 Markdown 产物准备完成",
                    stage="preparing_artifact",
                    artifact_path=artifact_path,
                )

            resource = ResourceRecord(
                document_id=document_id,
                workspace_id=task.workspace_id,
                user_id=task.user_id,
                workspace_name=task.workspace_name,
                source_type=source_type,  # type: ignore[arg-type]
                file_id=str(payload["file_id"]) if payload.get("file_id") else None,
                file_name=str(payload["file_name"]) if payload.get("file_name") else None,
                mime_type=str(payload["mime_type"]) if payload.get("mime_type") else None,
                content_hash=digest,
                size_bytes=int(payload["size_bytes"]),
                markdown_hash=content_hash(indexed_text.encode("utf-8")) if source_type == "file" else None,
                parser=parser,
                degraded=degraded,
                chunk_count=0,
                artifact_path=artifact_path,
                created_at=now,
                modified_at=now,
            )
            chunks = _build_chunks(runtime, task, resource, indexed_text)
            if not chunks:
                raise StageFailure("EMPTY_DOCUMENT", "parsing", 1, ValueError("resource produced no chunks"))
            resource.chunk_count = len(chunks)
            await runtime.task_log.write_task(
                task.task_id,
                "INFO",
                "Markdown 切分完成",
                stage="chunking",
                chunk_count=len(chunks),
                chunk_size=runtime.settings.chunk_size,
                chunk_overlap=runtime.settings.chunk_overlap,
            )
            current_workspace = await runtime.metadata.get_workspace(task.workspace_id)
            projected = _project_workspace(
                current_workspace,
                task,
                source_type=source_type,
                size_delta=resource.size_bytes,
                count_delta=1,
            )
            await _journal(runtime, task, resource=resource.model_dump(mode="json"))

            async def write_index() -> None:
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "清理同文档的历史索引记录",
                    stage="indexing",
                    document_id=document_id,
                )
                await asyncio.gather(
                    runtime.elasticsearch.delete_resource(task.workspace_id, document_id),
                    runtime.milvus.delete_resource(task.workspace_id, document_id),
                    return_exceptions=True,
                )
                embedding_started = time.monotonic()
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "开始生成文本向量",
                    stage="embedding",
                    chunk_count=len(chunks),
                )
                vectors = await runtime.embedding.embed([chunk.embedding_text for chunk in chunks])
                if len(vectors) != len(chunks):
                    raise RuntimeError("embedding count does not match chunk count")
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "文本向量生成完成",
                    stage="embedding",
                    vector_count=len(vectors),
                    vector_dimension=len(vectors[0]) if vectors else 0,
                    elapsed_ms=round((time.monotonic() - embedding_started) * 1000),
                )
                index_started = time.monotonic()
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "开始并行写入 Elasticsearch、Milvus 与 Workspace 镜像",
                    stage="indexing",
                    chunk_count=len(chunks),
                )
                await asyncio.gather(
                    runtime.elasticsearch.index_resource(resource, chunks),
                    runtime.milvus.upsert_chunks(chunks, vectors),
                    runtime.elasticsearch.mirror_workspace(projected),
                )
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "Elasticsearch、Milvus 与 Workspace 镜像写入完成",
                    stage="indexing",
                    elapsed_ms=round((time.monotonic() - index_started) * 1000),
                )
                count = await runtime.milvus.count_resource(task.workspace_id, document_id)
                if count != len(chunks):
                    raise RuntimeError(f"Milvus chunk count mismatch: expected={len(chunks)}, actual={count}")
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "Milvus 索引数量校验通过",
                    stage="indexing",
                    expected_count=len(chunks),
                    actual_count=count,
                )

            await _retry(
                runtime,
                task,
                stage="indexing",
                current=50,
                code="INDEX_WRITE_FAILED",
                operation=write_index,
            )
            await _journal(runtime, task, index_written=True)
            result = {
                "workspace_id": task.workspace_id,
                "workspace_name": task.workspace_name,
                "content_hash": digest,
                "size_bytes": resource.size_bytes,
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
            }
            if source_type == "file":
                result.update({"file_id": resource.file_id, "file_name": resource.file_name})
            await _publish_then_record_resource(
                runtime,
                task,
                resource,
                source_type=source_type,
                prepared=prepared,
                target=target,
                digest=digest,
                result=result,
            )
    except BaseException as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        error = _task_error(exc)
        compensation_errors = await _compensate_add(runtime, task, resource)
        await _fail(runtime, task, error, compensation_errors)
        if cancelled:
            raise
    finally:
        if file_slot_acquired:
            runtime.file_ingestion_semaphore.release()
        await runtime.tasks.release_content(task.user_id, task.workspace_id, source_type, digest, task.task_id)


async def _compensate_delete(
    runtime: Runtime,
    task: TaskRecord,
    resource: ResourceRecord,
    recycled: Path | None,
    backup: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    await _progress(runtime, task, "compensating", 80)
    try:
        if resource.source_type == "file" and recycled is not None and await asyncio.to_thread(recycled.exists):
            await runtime.artifacts.restore_from_recycle(recycled, resource.artifact_path)
        elif resource.source_type == "str":
            string_record = backup.get("string_record") if backup is not None else None
            if not isinstance(string_record, dict):
                raise RuntimeError("string delete backup is missing the JSONL record")
            await runtime.artifacts.restore_string_record(
                resource.user_id,
                resource.workspace_id,
                string_record,
            )
    except Exception as exc:
        errors.append(f"artifact restore: {exc}")
    if backup is not None and "es_chunks" in backup:
        try:
            es_chunks = [ChunkRecord.model_validate(value) for value in backup.get("es_chunks", [])]
            vector_chunks = [ChunkRecord.model_validate(value) for value in backup.get("vector_chunks", [])]
            vectors = backup.get("vectors", [])
            await asyncio.gather(
                runtime.elasticsearch.index_resource(resource, es_chunks),
                runtime.milvus.upsert_chunks(vector_chunks, vectors),
            )
        except Exception as exc:
            errors.append(f"index restore: {exc}")
    try:
        existing = await runtime.metadata.get_resource_by_document(resource.workspace_id, resource.document_id)
        if existing is None:
            resource.row_id = None
            await runtime.metadata.restore_resource(resource)
        workspace = await runtime.metadata.get_workspace(resource.workspace_id)
        if workspace is not None:
            await runtime.elasticsearch.mirror_workspace(workspace)
    except Exception as exc:
        errors.append(f"metadata restore: {exc}")
    if not errors:
        try:
            await runtime.artifacts.cleanup_recycle(task.task_id)
            await runtime.artifacts.cleanup_staging(task.task_id)
        except Exception as exc:
            errors.append(f"cleanup: {exc}")
    return errors


async def _process_delete(runtime: Runtime, task: TaskRecord) -> None:
    resource: ResourceRecord | None = None
    recycled: Path | None = None
    backup: dict[str, Any] | None = None
    cancelled = False
    is_string = task.operation == "delete_string"
    try:
        await runtime.metadata.update_task(task.task_id, started_at=local_now(), status=TaskStatus.RUNNING)
        async with runtime.tasks.workspace_lock(task.user_id, task.workspace_id):
            resource = (
                await runtime.metadata.get_string(task.workspace_id, str(task.payload["content_hash"]))
                if is_string
                else await runtime.metadata.get_file(task.workspace_id, str(task.payload["file_id"]))
            )
            if resource is None:
                if not task.payload.get("allow_missing"):
                    raise DomainError(
                        "string_not_found" if is_string else "file_not_found",
                        "string does not exist" if is_string else "file does not exist",
                        status_code=404,
                    )
                await runtime.task_log.write_task(
                    task.task_id,
                    "INFO",
                    "目标字符串已不存在，按幂等删除完成" if is_string else "目标文件已不存在，按幂等删除完成",
                    operation=task.operation,
                    stage="finalizing",
                    user_id=task.user_id,
                    workspace_id=task.workspace_id,
                    file_id=task.payload.get("file_id"),
                    file_name=task.payload.get("file_name"),
                    content_hash=task.payload.get("content_hash"),
                    already_absent=True,
                )
                now = local_now()
                await _complete(
                    runtime,
                    task,
                    {
                        "workspace_id": task.workspace_id,
                        "file_id": task.payload.get("file_id"),
                        "file_name": task.payload.get("file_name"),
                        "content_hash": task.payload.get("content_hash"),
                        "deleted": True,
                        "already_absent": True,
                        "deleted_at": now.isoformat(),
                    },
                )
                await runtime.artifacts.cleanup_staging(task.task_id)
                return
            if not is_string and resource.file_name != task.payload.get("file_name"):
                raise DomainError("file_name_mismatch", "file_name does not match the current file", status_code=409)
            await _progress(runtime, task, "preparing", 10)
            if is_string:
                try:
                    string_record = await runtime.artifacts.read_string_record(
                        task.user_id,
                        task.workspace_id,
                        resource.content_hash,
                    )
                    if string_record is None:
                        raise FileNotFoundError(resource.artifact_path)
                    backup = {
                        "resource": resource.model_dump(mode="json"),
                        "string_record": string_record,
                    }
                    await runtime.artifacts.write_delete_backup(task.task_id, backup)
                    await runtime.artifacts.remove_string_record(
                        task.user_id,
                        task.workspace_id,
                        resource.content_hash,
                    )
                except Exception as exc:
                    raise StageFailure("STRING_ARTIFACT_DELETE_FAILED", "preparing", 1, exc) from exc
                await _journal(runtime, task, resource=resource.model_dump(mode="json"), string_artifact_deleted=True)
            else:
                try:
                    recycled = await runtime.artifacts.move_to_recycle(task.task_id, resource.artifact_path)
                except Exception as exc:
                    raise StageFailure("RECYCLE_MOVE_FAILED", "preparing", 1, exc) from exc
                await _journal(
                    runtime,
                    task,
                    resource=resource.model_dump(mode="json"),
                    recycled_path=str(recycled),
                )

            async def capture_index_backup() -> dict[str, Any]:
                es_chunks, vector_data = await asyncio.gather(
                    runtime.elasticsearch.get_chunks(task.workspace_id, resource.document_id),
                    runtime.milvus.get_resource_chunks(task.workspace_id, resource.document_id),
                )
                vector_chunks, vectors = vector_data
                value = {
                    "resource": resource.model_dump(mode="json"),
                    "es_chunks": [chunk.model_dump(mode="json") for chunk in es_chunks],
                    "vector_chunks": [chunk.model_dump(mode="json") for chunk in vector_chunks],
                    "vectors": vectors,
                }
                if is_string and backup is not None:
                    value["string_record"] = backup["string_record"]
                await runtime.artifacts.write_delete_backup(task.task_id, value)
                return value

            backup = await _retry(
                runtime,
                task,
                stage="indexing",
                current=50,
                code="INDEX_SNAPSHOT_FAILED",
                operation=capture_index_backup,
            )
            current_workspace = await runtime.metadata.get_workspace(task.workspace_id)
            assert current_workspace is not None
            projected = _project_workspace(
                current_workspace,
                task,
                source_type=resource.source_type,
                size_delta=-resource.size_bytes,
                count_delta=-1,
            )

            async def delete_index() -> None:
                await asyncio.gather(
                    runtime.elasticsearch.delete_resource(task.workspace_id, resource.document_id),
                    runtime.milvus.delete_resource(task.workspace_id, resource.document_id),
                    runtime.elasticsearch.mirror_workspace(projected),
                )

            await _retry(
                runtime,
                task,
                stage="indexing",
                current=50,
                code="INDEX_DELETE_FAILED",
                operation=delete_index,
            )
            await _journal(runtime, task, index_deleted=True)
            await _retry(
                runtime,
                task,
                stage="metadata",
                current=75,
                code="DATABASE_STATS_UPDATE_FAILED",
                operation=lambda: runtime.metadata.delete_resource(resource),
            )
            await _journal(runtime, task, metadata_deleted=True)
            await _retry(
                runtime,
                task,
                stage="finalizing",
                current=90,
                code="RECYCLE_CLEANUP_FAILED",
                operation=lambda: runtime.artifacts.cleanup_recycle(task.task_id),
            )
            await _journal(runtime, task, recycle_cleaned=True)
            now = local_now()
            await _complete(
                runtime,
                task,
                {
                    "workspace_id": task.workspace_id,
                    "file_id": resource.file_id,
                    "file_name": resource.file_name,
                    "content_hash": resource.content_hash if is_string else None,
                    "document_id": str(resource.document_id),
                    "deleted": True,
                    "deleted_at": now.isoformat(),
                },
            )
            await runtime.artifacts.cleanup_staging(task.task_id)
    except BaseException as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        error = _task_error(exc)
        compensation_errors: list[str] = []
        if resource is not None and (recycled is not None or backup is not None):
            compensation_errors = await _compensate_delete(runtime, task, resource, recycled, backup)
        else:
            await runtime.artifacts.cleanup_staging(task.task_id)
        await _fail(runtime, task, error, compensation_errors)
        if cancelled:
            raise
    finally:
        if is_string:
            digest = str(task.payload.get("content_hash", ""))
            if digest:
                await runtime.tasks.release_content(
                    task.user_id,
                    task.workspace_id,
                    "str",
                    digest,
                    task.task_id,
                )
        else:
            file_id = str(task.payload.get("file_id", ""))
            if file_id:
                await runtime.tasks.release_file(task.user_id, task.workspace_id, file_id, task.task_id)


async def _process_read(runtime: Runtime, task: TaskRecord) -> None:
    try:
        await runtime.metadata.update_task(task.task_id, started_at=local_now(), status=TaskStatus.RUNNING)
        await _progress(runtime, task, "validating", 10)
        if task.operation == "list_workspaces":
            await _progress(runtime, task, "querying", 50)
            response = await runtime.metadata.list_workspaces(task.user_id)
        elif task.operation == "list_files":
            from jd_knowledge.services.files import FileService

            await _progress(runtime, task, "querying", 50)
            response = await FileService(runtime).list_files(
                task.user_id,
                task.workspace_id,
                include_string_content=bool(task.payload.get("include_string_content", False)),
            )
        elif task.operation == "retrieval":
            from jd_knowledge.services.retrieval import retrieve

            request = RetrievalRequest(
                user_id=task.user_id,
                workspace_id=task.workspace_id,
                query=str(task.payload["query"]),
                top_k=task.payload.get("top_k"),
            )
            await _progress(runtime, task, "retrieving", 50)
            response = await retrieve(runtime, request)
        else:  # pragma: no cover
            raise RuntimeError(f"unsupported read task operation: {task.operation}")
        await _progress(runtime, task, "serializing", 75)
        result = response.model_dump(mode="json", exclude_none=True)
        await _progress(runtime, task, "finalizing", 90)
        await _complete(runtime, task, result)
    except BaseException as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        await _fail(runtime, task, _task_error(exc), [])
        if cancelled:
            raise


async def process_task(runtime: Runtime, task_id: str) -> None:
    task = await runtime.metadata.get_task(task_id)
    if task is None or task.status != TaskStatus.QUEUED:
        return
    await runtime.task_log.write_task(
        task_id,
        "INFO",
        "任务开始处理",
        operation=task.operation,
        user_id=task.user_id,
        workspace_id=task.workspace_id,
        workspace_name=task.workspace_name,
    )
    if task.operation in {"add_file", "add_str"}:
        await _process_add(runtime, task)
    elif task.operation in {"delete_file", "delete_string"}:
        await _process_delete(runtime, task)
    elif task.operation in {"list_workspaces", "list_files", "retrieval"}:
        await _process_read(runtime, task)


async def recover_interrupted_task(runtime: Runtime, task: TaskRecord) -> None:
    await runtime.task_log.write_task(
        task.task_id,
        "WARN",
        "检测到中断任务，开始补偿恢复",
        operation=task.operation,
        workspace_id=task.workspace_id,
    )
    error = TaskError(
        code="PROCESS_INTERRUPTED",
        stage="interrupted",
        message="worker process stopped before the operation completed",
        attempts=1,
    )
    errors: list[str]
    if task.operation in {"add_file", "add_str"}:
        resource_value = task.journal.get("resource")
        resource = ResourceRecord.model_validate(resource_value) if resource_value else None
        errors = await _compensate_add(runtime, task, resource)
        await runtime.tasks.release_content(
            task.user_id,
            task.workspace_id,
            str(task.payload.get("source_type", "")),
            str(task.payload.get("content_hash", "")),
            task.task_id,
        )
    elif task.operation in {"delete_file", "delete_string"}:
        backup = await runtime.artifacts.read_delete_backup(task.task_id)
        resource_value = task.journal.get("resource") or (backup or {}).get("resource")
        resource = ResourceRecord.model_validate(resource_value) if resource_value else None
        recycled_value = task.journal.get("recycled_path")
        recycled = Path(recycled_value) if recycled_value else None
        errors = (
            await _compensate_delete(runtime, task, resource, recycled, backup)
            if resource is not None
            else ["delete recovery journal is incomplete"]
        )
        if task.operation == "delete_string":
            digest = str(task.payload.get("content_hash", ""))
            if digest:
                await runtime.tasks.release_content(
                    task.user_id,
                    task.workspace_id,
                    "str",
                    digest,
                    task.task_id,
                )
        else:
            file_id = str(task.payload.get("file_id", ""))
            if file_id:
                await runtime.tasks.release_file(task.user_id, task.workspace_id, file_id, task.task_id)
    else:
        errors = []
    await _fail(runtime, task, error, errors)


async def cancel_ingestion_task(runtime: Runtime, task: TaskRecord) -> None:
    """取消未在本进程执行的入库任务，并清理临时产物与内容占用。"""

    if task.operation not in {"add_file", "add_str"}:
        raise ValueError("only ingestion tasks can be cancelled")
    await runtime.task_log.write_task(
        task.task_id,
        "WARN",
        "用户取消入库任务，开始清理临时产物",
        operation=task.operation,
        workspace_id=task.workspace_id,
    )
    resource_value = task.journal.get("resource")
    resource = ResourceRecord.model_validate(resource_value) if resource_value else None
    errors = await _compensate_add(runtime, task, resource)
    await runtime.tasks.release_content(
        task.user_id,
        task.workspace_id,
        str(task.payload.get("source_type", "")),
        str(task.payload.get("content_hash", "")),
        task.task_id,
    )
    await _fail(
        runtime,
        task,
        TaskError(
            code="TASK_CANCELLED",
            stage="cancelled",
            message="task was cancelled by the user",
        ),
        errors,
    )


# Compatibility wrappers for internal callers while the v2 worker entrypoint is removed.
async def process_add(runtime: Runtime, task_id: str) -> None:
    await process_task(runtime, task_id)


async def process_delete(runtime: Runtime, task_id: str) -> None:
    await process_task(runtime, task_id)
