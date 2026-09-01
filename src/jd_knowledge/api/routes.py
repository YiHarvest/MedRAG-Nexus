"""保持向后兼容的公共 ``/api/v1`` 路由。"""

from __future__ import annotations

from types import ModuleType
from typing import Annotated

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from jd_knowledge.core.models import (
    ChatRequest,
    DeleteFileRequest,
    DeleteStringRequest,
    FileListResponse,
    FileSource,
    HealthResponse,
    RetrievalRequest,
    RetrievalResponse,
    TaskAccepted,
    TaskResponse,
    UserCreateRequest,
    UserListItem,
    UserListResponse,
    WorkspaceListResponse,
)

from .contracts import (
    ADD_REQUEST_BODY,
    HEALTH_UNAVAILABLE,
    INTERNAL_ERROR,
    QUEUE_ERROR,
    VALIDATION_ERROR,
    documented_error,
)
from .http import api_log, runtime_from
from .uploads import parse_add_request


def create_public_router(*, max_file_bytes: int, services: ModuleType) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/add",
        response_model=TaskAccepted,
        status_code=202,
        tags=["知识新增"],
        summary="异步新增文件或字符串",
        responses={
            409: documented_error(
                "文件、文件名或字符串重复。", "duplicate_file", "the same file has already been uploaded"
            ),
            413: documented_error(
                "文件或字符串超过限制。",
                "payload_too_large",
                "payload exceeds the configured limit",
            ),
            415: documented_error(
                "只接受 multipart。", "unsupported_media_type", "Content-Type must be multipart/form-data"
            ),
            422: VALIDATION_ERROR,
            503: QUEUE_ERROR,
            500: INTERNAL_ERROR,
        },
        openapi_extra={"requestBody": ADD_REQUEST_BODY},
    )
    async def add_knowledge(request: Request) -> TaskAccepted:
        payload = await parse_add_request(request, max_file_bytes=max_file_bytes)
        response = await services.FileService(runtime_from(request)).submit_add(payload)
        await api_log(
            request,
            "INFO",
            "新增知识任务提交成功",
            task_id=response.task_id,
            user_id=payload.user_id,
            workspace_id=payload.workspace_id,
            source_type=payload.source.type,
            file_name=payload.source.file_name if isinstance(payload.source, FileSource) else None,
        )
        return response

    @router.get(
        "/api/v1/users",
        response_model=UserListResponse,
        tags=["列表"],
        summary="列出已有用户",
        responses={500: INTERNAL_ERROR},
    )
    async def list_users(request: Request) -> UserListResponse:
        response = await services.FileService(runtime_from(request)).list_users()
        await api_log(request, "INFO", "用户列表查询完成", user_count=len(response.users))
        return response

    @router.post(
        "/api/v1/users",
        response_model=UserListItem,
        status_code=201,
        tags=["列表"],
        summary="新建用户",
        responses={409: documented_error("用户 ID 冲突。", "user_id_conflict", "user_id already exists")},
    )
    async def create_user(payload: Annotated[UserCreateRequest, Body()], request: Request) -> UserListItem:
        response = await services.FileService(runtime_from(request)).create_user(payload)
        await api_log(request, "INFO", "用户创建完成", user_id=response.user_id)
        return response

    @router.get(
        "/api/v1/users/{user_id}/workspaces",
        response_model=WorkspaceListResponse,
        tags=["列表"],
        summary="列出用户的 Workspace",
        responses={
            422: VALIDATION_ERROR,
            503: documented_error(
                "资源完整性暂时无法校验。",
                "resource_validation_unavailable",
                "resource completeness could not be verified",
            ),
            500: INTERNAL_ERROR,
        },
    )
    async def list_workspaces(
        user_id: Annotated[str, Path(min_length=1, max_length=128, description="用户业务标识。")],
        request: Request,
    ) -> WorkspaceListResponse:
        response = await services.FileService(runtime_from(request)).list_workspaces(user_id)
        await api_log(
            request,
            "INFO",
            "Workspace 列表查询完成",
            user_id=user_id,
            workspace_count=len(response.workspaces),
        )
        return response

    @router.get(
        "/api/v1/workspaces/{workspace_id}/files",
        response_model=FileListResponse,
        response_model_exclude_none=True,
        tags=["列表"],
        summary="列出 Workspace 文件",
        responses={
            404: documented_error("Workspace 不存在。", "workspace_not_found", "workspace does not exist"),
            503: documented_error(
                "资源完整性暂时无法校验。",
                "resource_validation_unavailable",
                "resource completeness could not be verified",
            ),
            500: INTERNAL_ERROR,
        },
    )
    async def list_files(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[\w:@-]+$")],
        request: Request,
        user_id: Annotated[str, Query(min_length=1, max_length=128, description="Workspace 所属用户 ID。")],
        include_string_content: Annotated[
            bool,
            Query(description="是否返回 strings[].content；默认仅返回字符串元数据。"),
        ] = False,
    ) -> FileListResponse:
        response = await services.FileService(runtime_from(request)).list_files(
            user_id,
            workspace_id,
            include_string_content=include_string_content,
        )
        await api_log(
            request,
            "INFO",
            "Workspace 资源列表查询完成",
            user_id=user_id,
            workspace_id=workspace_id,
            file_count=len(response.files),
            string_count=len(response.strings),
            resource_count=response.stats.resource_count,
            include_string_content=include_string_content,
        )
        return response

    @router.post(
        "/api/v1/delete",
        response_model=TaskAccepted,
        status_code=202,
        tags=["删除"],
        summary="异步删除文件",
        responses={
            404: documented_error("Workspace 不存在。", "workspace_not_found", "workspace does not exist"),
            409: documented_error("文件名不匹配或文件正忙。", "file_busy", "the file already has an active write task"),
            422: VALIDATION_ERROR,
            503: QUEUE_ERROR,
            500: INTERNAL_ERROR,
        },
    )
    async def delete_file(payload: Annotated[DeleteFileRequest, Body()], request: Request) -> TaskAccepted:
        response = await services.FileService(runtime_from(request)).submit_delete(payload)
        await api_log(
            request,
            "INFO",
            "删除文件任务提交成功",
            task_id=response.task_id,
            user_id=payload.user_id,
            workspace_id=payload.workspace_id,
            file_id=payload.file_id,
            file_name=payload.file_name,
        )
        return response

    @router.post(
        "/api/v1/delete-string",
        response_model=TaskAccepted,
        status_code=202,
        tags=["删除"],
        summary="异步删除字符串知识",
        responses={
            404: documented_error("Workspace 不存在。", "workspace_not_found", "workspace does not exist"),
            409: documented_error(
                "字符串正忙或 Workspace 需要修复。",
                "string_busy",
                "the string already has an active write task",
            ),
            422: VALIDATION_ERROR,
            503: QUEUE_ERROR,
            500: INTERNAL_ERROR,
        },
    )
    async def delete_string(payload: DeleteStringRequest, request: Request) -> TaskAccepted:
        response = await services.FileService(runtime_from(request)).submit_delete_string(payload)
        await api_log(
            request,
            "INFO",
            "删除字符串任务提交成功",
            task_id=response.task_id,
            user_id=payload.user_id,
            workspace_id=payload.workspace_id,
            content_hash=payload.content_hash,
        )
        return response

    @router.post(
        "/api/v1/retrieval",
        response_model=RetrievalResponse,
        response_model_exclude_none=True,
        tags=["检索"],
        summary="混合召回知识",
        responses={
            404: documented_error("Workspace 不存在。", "workspace_not_found", "workspace does not exist"),
            422: VALIDATION_ERROR,
            503: documented_error(
                "召回通道均不可用。", "retrieval_unavailable", "both retrieval paths are unavailable"
            ),
            500: INTERNAL_ERROR,
        },
    )
    async def retrieve_knowledge(payload: RetrievalRequest, request: Request) -> RetrievalResponse:
        response = await services.retrieve(runtime_from(request), payload)
        await api_log(
            request,
            "INFO",
            "混合检索完成",
            user_id=payload.user_id,
            workspace_id=payload.workspace_id,
            query=payload.query,
            top_k=payload.top_k,
            result_count=response.count,
            degraded=response.degraded,
        )
        return response

    @router.post(
        "/api/v1/chat/stream",
        tags=["聊天"],
        summary="跨用户知识库流式聊天",
        response_class=StreamingResponse,
        responses={422: VALIDATION_ERROR, 500: INTERNAL_ERROR},
    )
    async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        await api_log(
            request,
            "INFO",
            "流式聊天请求已接受",
            user_id=payload.user_id,
            message_count=len(payload.messages),
            top_k=payload.top_k,
        )
        return StreamingResponse(
            services.stream_chat(runtime_from(request), payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResponse,
        tags=["任务"],
        summary="查询异步任务",
        responses={
            404: documented_error("任务不存在或不属于用户。", "task_not_found", "task does not exist"),
            422: VALIDATION_ERROR,
        },
    )
    async def get_task(
        task_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="异步任务 ID（32 位十六进制）。")],
        request: Request,
        user_id: Annotated[str, Query(min_length=1, max_length=128, description="提交任务时的用户业务标识。")],
    ) -> TaskResponse:
        response = await services.FileService(runtime_from(request)).get_task(task_id, user_id)
        await api_log(
            request,
            "INFO",
            "异步任务状态查询完成",
            task_id=task_id,
            user_id=user_id,
            status=response.status.value,
            stage=response.stage,
            progress=f"{response.progress.percent}%",
            error_code=response.error.code if response.error else None,
        )
        return response

    @router.get(
        "/api/v1/health/live",
        response_model=HealthResponse,
        tags=["健康检查"],
        summary="进程存活检查",
    )
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @router.get(
        "/api/v1/health/ready",
        response_model=HealthResponse,
        tags=["健康检查"],
        summary="服务就绪检查",
        responses={503: HEALTH_UNAVAILABLE},
    )
    async def ready(
        request: Request,
        details: Annotated[bool, Query(description="是否返回脱敏依赖详情。")] = False,
    ) -> JSONResponse | HealthResponse:
        response = (
            await services.dependency_health(runtime_from(request))
            if details
            else await services.readiness(runtime_from(request))
        )
        await api_log(
            request,
            "INFO" if response.status == "ok" else "WARN",
            "服务就绪检查完成",
            status=response.status,
            details=details,
            dependency_count=len(response.dependencies or {}),
            unavailable_dependencies=",".join(
                name for name, state in (response.dependencies or {}).items() if state.status == "unavailable"
            )
            or "-",
        )
        if response.status == "unavailable":
            return JSONResponse(status_code=503, content=response.model_dump(mode="json", exclude_none=True))
        return response

    return router


__all__ = ["create_public_router"]
