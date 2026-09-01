"""编排知识文件与字符串资源的提交、删除和查询。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import sqlite3
import unicodedata
from pathlib import Path

from medrag_nexus.core.ids import (
    content_hash,
    new_id,
    new_task_id,
    normalize_workspace_name,
    text_content_hash,
)
from medrag_nexus.core.ids import (
    file_id as new_file_id,
)
from medrag_nexus.core.models import (
    AddRequest,
    DeleteFileRequest,
    DeleteStringRequest,
    DomainError,
    FileListItem,
    FileListResponse,
    FileSource,
    ResourceRecord,
    StringListItem,
    StringSource,
    TaskAccepted,
    TaskError,
    TaskRecord,
    TaskResponse,
    TaskStatus,
    UserCreateRequest,
    UserListItem,
    UserListResponse,
    WorkspaceListResponse,
    WorkspaceStats,
    local_now,
)
from medrag_nexus.pipeline.parsers import validate_file_type
from medrag_nexus.services.callbacks import schedule_task_callback
from medrag_nexus.services.runtime import Runtime

SUPPORTED_FILE_EXTENSIONS = {".pdf", ".txt", ".docx"}
SUPPORTED_MIME_TYPES = {
    ".pdf": {"application/pdf"},
    ".txt": {"text/plain"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/wps-office.docx",
        "application/zip",
    },
}
GENERIC_MIME_TYPES = {"application/octet-stream", "binary/octet-stream"}


def normalize_file_name(value: str) -> str:
    name = unicodedata.normalize("NFC", value).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise DomainError("invalid_file_name", "file_name must be a plain file name", status_code=422)
    if len(name) > 255:
        raise DomainError("invalid_file_name", "file_name is too long", status_code=422)
    extension = Path(name).suffix.lower()
    if extension not in SUPPORTED_FILE_EXTENSIONS:
        raise DomainError(
            "unsupported_file_type",
            f"unsupported file extension: {extension or '<none>'}",
            status_code=415,
        )
    return name


def _decode_file(source: FileSource, max_bytes: int) -> tuple[str, bytes]:
    try:
        content = base64.b64decode(source.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DomainError("invalid_file", "uploaded file content is invalid", status_code=422) from exc
    if len(content) > max_bytes:
        raise DomainError("payload_too_large", "file exceeds the configured limit", status_code=413)
    file_name = normalize_file_name(source.file_name)
    extension = Path(file_name).suffix.lower()
    mime_type = source.mime_type.partition(";")[0].strip().lower()
    if mime_type not in GENERIC_MIME_TYPES and mime_type not in SUPPORTED_MIME_TYPES[extension]:
        raise DomainError(
            "unsupported_file_type",
            f"file MIME type does not match extension: extension={extension}, mime_type={mime_type}",
            status_code=415,
        )
    return file_name, content


class FileService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    @staticmethod
    def _workspace_identity(request: AddRequest) -> tuple[str, str]:
        workspace_name = normalize_workspace_name(request.workspace_name)
        return request.workspace_id, workspace_name

    async def _check_workspace(self, user_id: str, workspace_id: str, workspace_name: str) -> None:
        lifecycle = await self.runtime.metadata.workspace_lifecycle_state(workspace_id)
        if lifecycle in {"deleting", "delete_failed", "deleted"}:
            raise DomainError(
                "workspace_deleted",
                "workspace has been deleted and its identifier cannot be reused",
                status_code=409,
            )
        repair_reason = await self.runtime.metadata.workspace_repair_reason(workspace_id)
        if repair_reason:
            raise DomainError(
                "workspace_requires_repair",
                "workspace writes are blocked until consistency repair is completed",
                status_code=409,
                details={"reason": repair_reason},
            )
        existing = await self.runtime.metadata.get_workspace(workspace_id)
        if existing is None:
            return
        if existing.user_id != user_id or existing.workspace_name != workspace_name:
            raise DomainError(
                "workspace_identity_conflict",
                "workspace_id is already bound to another user_id or workspace_name",
                status_code=409,
            )

    @staticmethod
    def _duplicate_error(source_type: str, digest: str, duplicate: object) -> DomainError:
        if source_type == "file":
            return DomainError(
                "duplicate_file",
                "the same file content has already been uploaded",
                status_code=409,
                details={
                    "content_hash": digest,
                    "file_id": getattr(duplicate, "file_id", None),
                    "file_name": getattr(duplicate, "file_name", None),
                },
            )
        return DomainError(
            "duplicate_text",
            "the same text content has already been added",
            status_code=409,
            details={"content_hash": digest},
        )

    async def submit_add(self, request: AddRequest) -> TaskAccepted:
        workspace_id, workspace_name = self._workspace_identity(request)
        task_id = new_task_id()
        document_id = new_id()
        staged = False
        reserved = False
        digest = ""
        source_type = request.source.type
        try:
            if isinstance(request.source, FileSource):
                file_name, content = _decode_file(request.source, self.runtime.settings.max_file_bytes)
                digest = content_hash(content)
                source_path = await self.runtime.artifacts.stage_bytes(task_id, file_name, content)
                staged = True
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "上传文件已写入临时区",
                    operation="add_file",
                    user_id=request.user_id,
                    workspace_id=workspace_id,
                    file_name=file_name,
                    mime_type=request.source.mime_type,
                    size_bytes=len(content),
                    content_hash=digest,
                    staging_path=source_path,
                )
                try:
                    validate_file_type(source_path)
                except ValueError as exc:
                    raise DomainError("unsupported_file_type", str(exc), status_code=415) from exc
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "上传文件扩展名与文件内容校验通过",
                    operation="add_file",
                    file_name=file_name,
                    extension=source_path.suffix.lower(),
                )
                operation = "add_file"
                payload = {
                    "document_id": str(document_id),
                    "source_type": "file",
                    "source_path": str(source_path),
                    "file_id": new_file_id(),
                    "file_name": file_name,
                    "mime_type": request.source.mime_type,
                    "content_hash": digest,
                    "size_bytes": len(content),
                }
            elif isinstance(request.source, StringSource):
                encoded = request.source.content.encode("utf-8")
                if len(encoded) > self.runtime.settings.max_text_bytes:
                    raise DomainError(
                        "payload_too_large",
                        "string content exceeds the configured limit",
                        status_code=413,
                    )
                digest = text_content_hash(request.source.content)
                source_path = await self.runtime.artifacts.stage_text(task_id, request.source.content)
                staged = True
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "字符串知识已写入临时区",
                    operation="add_str",
                    user_id=request.user_id,
                    workspace_id=workspace_id,
                    size_bytes=len(encoded),
                    content_hash=digest,
                    staging_path=source_path,
                )
                operation = "add_str"
                payload = {
                    "document_id": str(document_id),
                    "source_type": "str",
                    "source_path": str(source_path),
                    "file_id": None,
                    "file_name": None,
                    "mime_type": "text/plain",
                    "content_hash": digest,
                    "size_bytes": len(encoded),
                }
            else:  # pragma: no cover
                raise DomainError("invalid_type", "unsupported source type", status_code=422)

            await self._require_redis()
            async with self.runtime.tasks.workspace_lock(
                request.user_id,
                workspace_id,
                wait_seconds=10,
            ):
                await self._check_workspace(request.user_id, workspace_id, workspace_name)
                duplicate = await self.runtime.metadata.find_duplicate(workspace_id, source_type, digest)
                if duplicate is not None:
                    raise self._duplicate_error(source_type, digest, duplicate)
                active = await self.runtime.tasks.reserve_content(
                    request.user_id,
                    workspace_id,
                    source_type,
                    digest,
                    task_id,
                )
                if active:
                    raise DomainError(
                        "upload_in_progress",
                        "the same content is already being processed",
                        status_code=409,
                        details={"active_task_id": active, "content_hash": digest},
                    )
                reserved = True
                task = TaskRecord(
                    task_id=task_id,
                    user_id=request.user_id,
                    workspace_id=workspace_id,
                    workspace_name=workspace_name,
                    operation=operation,
                    payload=payload,
                )
                if request.callback_url is not None:
                    task.payload["callback_url"] = str(request.callback_url)
                await self.runtime.metadata.create_task(task)
                try:
                    await self.runtime.tasks.enqueue(task_id)
                except Exception as exc:
                    await self.mark_submission_failed(task_id, "task could not be queued")
                    raise DomainError("redis_unavailable", "task could not be queued", status_code=503) from exc
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "入库任务已创建并进入 Redis 队列",
                    operation=operation,
                    user_id=request.user_id,
                    workspace_id=workspace_id,
                    workspace_name=workspace_name,
                    source_type=source_type,
                    file_name=payload.get("file_name"),
                    size_bytes=payload["size_bytes"],
                )
                schedule_task_callback(self.runtime, task)
            return TaskAccepted(task_id=task_id)
        except sqlite3.IntegrityError as exc:
            raise DomainError("duplicate_content", "the same content already exists", status_code=409) from exc
        except Exception:
            if staged:
                await self.runtime.artifacts.cleanup_staging(task_id)
            if reserved:
                await self.runtime.tasks.release_content(
                    request.user_id,
                    workspace_id,
                    source_type,
                    digest,
                    task_id,
                )
            raise

    async def cancel_ingestion(self, task_id: str, user_id: str) -> TaskResponse:
        """取消排队中或正在执行的文件/文本入库任务。"""

        task = await self.runtime.metadata.get_task(task_id)
        if task is None or task.user_id != user_id:
            raise DomainError("task_not_found", "task does not exist", status_code=404)
        if task.operation not in {"add_file", "add_str"}:
            raise DomainError("task_not_cancellable", "only ingestion tasks can be cancelled", status_code=409)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            return await self.get_task(task_id, user_id)

        if task.status == TaskStatus.QUEUED:
            removed = await self.runtime.tasks.cancel_queued(task_id)
            if not removed and await self.runtime.cancel_active_task(task_id):
                return await self.get_task(task_id, user_id)
        elif await self.runtime.cancel_active_task(task_id):
            return await self.get_task(task_id, user_id)

        # Worker 可能刚完成，或者这是服务重启后遗留的 running 记录；重新读取后再决定。
        current = await self.runtime.metadata.get_task(task_id)
        if current is None:
            raise DomainError("task_not_found", "task does not exist", status_code=404)
        if current.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            return await self.get_task(task_id, user_id)
        from medrag_nexus.services.processing import cancel_ingestion_task

        await cancel_ingestion_task(self.runtime, current)
        return await self.get_task(task_id, user_id)

    async def submit_delete(self, request: DeleteFileRequest) -> TaskAccepted:
        await self._require_redis()
        task_id = new_task_id()
        reserved = False
        async with self.runtime.tasks.workspace_lock(request.user_id, request.workspace_id, wait_seconds=10):
            repair_reason = await self.runtime.metadata.workspace_repair_reason(request.workspace_id)
            if repair_reason:
                raise DomainError(
                    "workspace_requires_repair",
                    "workspace writes are blocked until consistency repair is completed",
                    status_code=409,
                    details={"reason": repair_reason},
                )
            workspace = await self.runtime.metadata.get_workspace(request.workspace_id)
            if workspace is None or workspace.user_id != request.user_id:
                raise DomainError("workspace_not_found", "workspace does not exist", status_code=404)
            resource = await self.runtime.metadata.get_file(request.workspace_id, request.file_id)
            already_absent = resource is None
            if resource is not None and resource.file_name != request.file_name:
                raise DomainError(
                    "file_name_mismatch",
                    "file_name does not match the current file",
                    status_code=409,
                    details={"file_id": request.file_id},
                )
            await self.runtime.tasks.reserve_file(request.user_id, request.workspace_id, request.file_id, task_id)
            reserved = True
            try:
                task = TaskRecord(
                    task_id=task_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    workspace_name=workspace.workspace_name,
                    operation="delete_file",
                    payload={
                        "file_id": request.file_id,
                        "file_name": request.file_name,
                        "allow_missing": already_absent,
                        "callback_url": str(request.callback_url) if request.callback_url is not None else None,
                    },
                )
                await self.runtime.metadata.create_task(task)
                try:
                    await self.runtime.tasks.enqueue(task_id)
                except Exception as exc:
                    await self.mark_submission_failed(task_id, "task could not be queued")
                    raise DomainError("redis_unavailable", "task could not be queued", status_code=503) from exc
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "删除任务已创建并进入 Redis 队列",
                    operation="delete_file",
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    file_id=request.file_id,
                    file_name=request.file_name,
                    already_absent=already_absent,
                )
                schedule_task_callback(self.runtime, task)
            except Exception:
                if reserved:
                    await self.runtime.tasks.release_file(
                        request.user_id,
                        request.workspace_id,
                        request.file_id,
                        task_id,
                    )
                raise
        return TaskAccepted(task_id=task_id)

    async def submit_delete_string(self, request: DeleteStringRequest) -> TaskAccepted:
        await self._require_redis()
        task_id = new_task_id()
        reserved = False
        async with self.runtime.tasks.workspace_lock(request.user_id, request.workspace_id, wait_seconds=10):
            repair_reason = await self.runtime.metadata.workspace_repair_reason(request.workspace_id)
            if repair_reason:
                raise DomainError(
                    "workspace_requires_repair",
                    "workspace writes are blocked until consistency repair is completed",
                    status_code=409,
                    details={"reason": repair_reason},
                )
            workspace = await self.runtime.metadata.get_workspace(request.workspace_id)
            if workspace is None or workspace.user_id != request.user_id:
                raise DomainError("workspace_not_found", "workspace does not exist", status_code=404)
            resource = await self.runtime.metadata.get_string(request.workspace_id, request.content_hash)
            already_absent = resource is None
            active = await self.runtime.tasks.reserve_content(
                request.user_id,
                request.workspace_id,
                "str",
                request.content_hash,
                task_id,
            )
            if active:
                raise DomainError(
                    "string_busy",
                    "the string already has an active write task",
                    status_code=409,
                    details={"active_task_id": active, "content_hash": request.content_hash},
                )
            reserved = True
            try:
                task = TaskRecord(
                    task_id=task_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    workspace_name=workspace.workspace_name,
                    operation="delete_string",
                    payload={
                        "content_hash": request.content_hash,
                        "allow_missing": already_absent,
                        "callback_url": str(request.callback_url) if request.callback_url is not None else None,
                    },
                )
                await self.runtime.metadata.create_task(task)
                try:
                    await self.runtime.tasks.enqueue(task_id)
                except Exception as exc:
                    await self.mark_submission_failed(task_id, "task could not be queued")
                    raise DomainError("redis_unavailable", "task could not be queued", status_code=503) from exc
                await self.runtime.task_log.write_task(
                    task_id,
                    "INFO",
                    "字符串删除任务已创建并进入 Redis 队列",
                    operation="delete_string",
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    content_hash=request.content_hash,
                    already_absent=already_absent,
                )
                schedule_task_callback(self.runtime, task)
            except Exception:
                if reserved:
                    await self.runtime.tasks.release_content(
                        request.user_id,
                        request.workspace_id,
                        "str",
                        request.content_hash,
                        task_id,
                    )
                raise
        return TaskAccepted(task_id=task_id)

    async def list_workspaces(self, user_id: str) -> WorkspaceListResponse:
        response = await self.runtime.metadata.list_workspaces(user_id)
        workspaces = []
        for workspace in response.workspaces:
            records = await self._visible_resources(user_id, workspace.workspace_id)
            stats = self._stats(records)
            workspaces.append(
                workspace.model_copy(
                    update={
                        "resource_count": stats.resource_count,
                        "file_count": stats.file_count,
                        "str_count": stats.str_count,
                        "total_size_bytes": stats.total_size_bytes,
                    }
                )
            )
        return response.model_copy(update={"workspaces": workspaces})

    async def list_users(self) -> UserListResponse:
        """列出 SQLite 中已有用户；不改变 API/MCP 的授权边界。"""

        return await self.runtime.metadata.list_users()

    async def create_user(self, request: UserCreateRequest) -> UserListItem:
        """保存调用方提供 ID 的空用户（供 MCP 等受信任集成使用）。"""

        created = await self.runtime.metadata.create_user(request.user_id, request.user_name)
        if created is None:
            raise DomainError("user_id_conflict", "user_id already exists", status_code=409)
        return created

    async def list_files(
        self,
        user_id: str,
        workspace_id: str,
        *,
        include_string_content: bool = False,
    ) -> FileListResponse:
        workspace = await self.runtime.metadata.get_workspace(workspace_id)
        if workspace is None or workspace.user_id != user_id:
            raise DomainError("workspace_not_found", "workspace does not exist", status_code=404)
        records = await self._visible_resources(user_id, workspace_id)
        files = [
            FileListItem(
                file_id=resource.file_id,
                file_name=resource.file_name,
                content_hash=resource.content_hash,
                size_bytes=resource.size_bytes,
                created_at=resource.created_at,
                modified_at=resource.modified_at,
            )
            for resource in records
            if resource.source_type == "file" and resource.file_id is not None and resource.file_name is not None
        ]
        strings = [
            StringListItem(
                content_hash=resource.content_hash,
                size_bytes=resource.size_bytes,
                created_at=resource.created_at,
                modified_at=resource.modified_at,
            )
            for resource in records
            if resource.source_type == "str"
        ]
        stats = self._stats(records)
        if include_string_content and strings:
            contents = await self.runtime.artifacts.read_string_contents(workspace.user_id, workspace_id)
            strings = [item.model_copy(update={"content": contents.get(item.content_hash)}) for item in strings]
        return FileListResponse(workspace_id=workspace_id, files=files, strings=strings, stats=stats)

    @staticmethod
    def _stats(resources: list[ResourceRecord]) -> WorkspaceStats:
        return WorkspaceStats(
            resource_count=len(resources),
            file_count=sum(resource.source_type == "file" for resource in resources),
            str_count=sum(resource.source_type == "str" for resource in resources),
            total_size_bytes=sum(resource.size_bytes for resource in resources),
        )

    async def _visible_resources(self, user_id: str, workspace_id: str) -> list[ResourceRecord]:
        records, incomplete_records = await asyncio.gather(
            self.runtime.metadata.list_resource_records(workspace_id),
            self.runtime.metadata.incomplete_resource_records(workspace_id),
        )
        for resource in incomplete_records:
            try:
                await self.runtime.task_log.write_api(
                    "WARN",
                    "新增任务未达到 succeeded/100%，列表响应已隐藏该资源",
                    user_id=user_id,
                    workspace_id=workspace_id,
                    document_id=resource.document_id,
                    file_id=resource.file_id,
                    file_name=resource.file_name,
                    incomplete_parts="task_not_succeeded_100",
                )
            except Exception:
                pass
        if not records:
            return []

        async def validate(resource: ResourceRecord) -> tuple[ResourceRecord, list[str]]:
            try:
                artifact_ok, es_resource, es_count, milvus_count = await asyncio.gather(
                    self.runtime.artifacts.resource_is_complete(resource),
                    self.runtime.elasticsearch.get_resource(workspace_id, resource.document_id),
                    self.runtime.elasticsearch.count_document_chunks(workspace_id, resource.document_id),
                    self.runtime.milvus.count_resource(workspace_id, resource.document_id),
                )
            except Exception as exc:
                raise DomainError(
                    "resource_validation_unavailable",
                    "resource completeness could not be verified",
                    status_code=503,
                ) from exc
            reasons = []
            if not artifact_ok:
                reasons.append("artifact")
            if es_resource is None:
                reasons.append("elasticsearch_resource")
            if es_count != resource.chunk_count:
                reasons.append("elasticsearch_chunks")
            if milvus_count != resource.chunk_count:
                reasons.append("milvus_chunks")
            return resource, reasons

        checked = await asyncio.gather(*(validate(resource) for resource in records))
        visible = []
        for resource, reasons in checked:
            if not reasons:
                visible.append(resource)
                continue
            try:
                await self.runtime.task_log.write_api(
                    "WARN",
                    "资源完整性校验失败，列表响应已隐藏该资源",
                    user_id=user_id,
                    workspace_id=workspace_id,
                    document_id=resource.document_id,
                    file_id=resource.file_id,
                    file_name=resource.file_name,
                    incomplete_parts=",".join(reasons),
                )
            except Exception:
                pass
        return visible

    async def get_task(self, task_id: str, user_id: str) -> TaskResponse:
        task = await self.runtime.metadata.get_task(task_id)
        if task is None or task.user_id != user_id:
            raise DomainError("task_not_found", "task does not exist", status_code=404)
        public = task.model_dump(
            exclude={"operation", "payload", "journal", "user_id", "workspace_id", "workspace_name"}
        )
        return TaskResponse.model_validate(public)

    async def _require_redis(self) -> None:
        try:
            await self.runtime.tasks.health()
        except Exception as exc:
            raise DomainError("redis_unavailable", "Redis is unavailable", status_code=503) from exc

    async def mark_submission_failed(self, task_id: str, message: str) -> None:
        await self.runtime.metadata.update_task(
            task_id,
            status=TaskStatus.FAILED,
            stage="submission",
            error=TaskError(code="QUEUE_WRITE_FAILED", stage="submission", message=message, attempts=1),
            finished_at=local_now(),
        )
        await self.runtime.task_log.write_task(
            task_id,
            "ERROR",
            "任务提交失败",
            stage="submission",
            error=message,
        )
