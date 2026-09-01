"""WebUI Agent 的确认、输入与临时制品路由。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

from medrag_nexus.core.models import APIModel
from medrag_nexus.core.paths import AGENT_API_PREFIX
from medrag_nexus.webui.policy_store import KnowledgePolicyStore
from medrag_nexus.webui.router import DEFAULT_COOKIE_NAME, WebUiPrincipal, create_principal_dependency
from medrag_nexus.webui.store import WebUiStore

from .artifacts import ArtifactService
from .models import ActionTarget, AgentAction
from .service import (
    AgentExecutionError,
    ConfirmedActionExecutor,
    action_response,
    file_input,
    secure_input,
)
from .store import (
    ActionNotFoundError,
    ActionOwnershipError,
    ActionPayloadError,
    ActionStateError,
    AgentStore,
    AgentStoreError,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    InvalidConfirmationError,
)


class ConfirmActionRequest(APIModel):
    confirmation_text: str | None = Field(default=None, max_length=256)


class SecureInputRequest(APIModel):
    values: dict[str, str] = Field(default_factory=dict)


def create_agent_router(
    runtime: Any,
    webui_store: WebUiStore,
    policies: KnowledgePolicyStore,
    action_store: AgentStore,
    artifacts: ArtifactService,
    *,
    cookie_name: str = DEFAULT_COOKIE_NAME,
) -> APIRouter:
    """构建仅供已登录 WebUI 会话使用的 Agent 路由。"""

    principal_dependency = create_principal_dependency(webui_store, cookie_name=cookie_name)
    principal_dep = Depends(principal_dependency)
    executor = ConfirmedActionExecutor(cookie_name=cookie_name)
    router = APIRouter(prefix=AGENT_API_PREFIX, tags=["Agent"])

    @router.get("/actions/{action_id}")
    async def get_action(action_id: str, caller: WebUiPrincipal = principal_dep) -> dict[str, Any]:
        action = await _owned_action(action_store, action_id, caller)
        return action_response(action)

    @router.post("/actions/{action_id}/confirm")
    async def confirm_action(
        action_id: str,
        payload: ConfirmActionRequest,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        action = await _owned_action(action_store, action_id, caller)
        if action.status in {"executing", "succeeded", "failed"}:
            return action_response(action)
        if action.status in {"cancelled", "expired"}:
            raise _store_error(ActionStateError(f"cannot confirm action in {action.status} state"))
        await _authorize_action(runtime, policies, action, caller, verify_target=True)
        confirmed = action
        if action.status == "pending":
            try:
                confirmed = await action_store.confirm_action(
                    action_id,
                    account_id=caller.account_id,
                    confirmation_text=payload.confirmation_text,
                )
            except AgentStoreError as exc:
                raise _store_error(exc) from exc
        if confirmed.tool_name in {
            "change_own_password",
            "create_account",
            "request_file_upload",
            "reset_account_password",
        }:
            input_payload = (
                file_input(confirmed.action_id)
                if confirmed.tool_name == "request_file_upload"
                else secure_input(confirmed.action_id, confirmed.tool_name)
            )
            return action_response(
                confirmed,
                input=input_payload,
            )
        return await _execute_confirmed(
            request,
            caller,
            confirmed,
            action_store,
            executor,
        )

    @router.delete("/actions/{action_id}")
    async def cancel_action(action_id: str, caller: WebUiPrincipal = principal_dep) -> dict[str, Any]:
        try:
            cancelled = await action_store.cancel_action(action_id, account_id=caller.account_id)
        except AgentStoreError as exc:
            raise _store_error(exc) from exc
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.agent.action.cancel",
            resource_type="agent_action",
            resource_id=action_id,
        )
        return action_response(cancelled)

    @router.post("/actions/{action_id}/input")
    async def submit_input(
        action_id: str,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        action = await _owned_action(action_store, action_id, caller)
        await _authorize_action(runtime, policies, action, caller, verify_target=False)
        if action.status != "confirmed":
            raise _error(status.HTTP_409_CONFLICT, "invalid_action_state", "操作尚未确认或已处理")
        content_type = request.headers.get("content-type", "").casefold()
        if "multipart/form-data" in content_type:
            if action.tool_name != "request_file_upload":
                raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "此操作不接受文件")
            form = await request.form()
            uploads = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
            if len(uploads) != 1:
                raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "必须选择一个文件")
            uploaded = uploads[0]
            content = await uploaded.read(runtime.settings.max_file_bytes + 1)
            if len(content) > runtime.settings.max_file_bytes:
                raise _error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload_too_large", "文件超过大小限制")
            upload = (
                Path(uploaded.filename or "upload.bin").name,
                uploaded.content_type or "application/octet-stream",
                content,
            )
            return await _execute_confirmed(
                request,
                caller,
                action,
                action_store,
                executor,
                upload=upload,
            )
        if action.tool_name not in {"change_own_password", "create_account", "reset_account_password"}:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "此操作不接受安全表单")
        try:
            body = SecureInputRequest.model_validate(await request.json())
        except Exception as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "安全表单格式无效") from exc
        allowed_fields = (
            {"current_password", "new_password"} if action.tool_name == "change_own_password" else {"new_password"}
        )
        if set(body.values) != allowed_fields:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "安全表单字段无效")
        return await _execute_confirmed(
            request,
            caller,
            action,
            action_store,
            executor,
            secure_values=body.values,
        )

    @router.get("/artifacts/{artifact_id}/download")
    async def download_artifact(
        artifact_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> FileResponse:
        try:
            artifact, path = await artifacts.resolve_download(artifact_id)
        except (ArtifactNotFoundError, ArtifactUnavailableError) as exc:
            raise _artifact_error(exc) from exc
        await _authorize_artifact(runtime, policies, artifact, caller)
        await action_store.record_artifact_download(artifact_id, account_id=caller.account_id)
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.agent.artifact.download",
            resource_type="agent_artifact",
            resource_id=artifact_id,
            after={"file_name": artifact.file_name, "owner_account_id": artifact.owner_account_id},
        )
        return FileResponse(
            path,
            media_type=artifact.mime_type,
            filename=artifact.file_name,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.delete("/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_artifact(
        artifact_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> None:
        try:
            record = await action_store.get_artifact(artifact_id)
        except ArtifactNotFoundError as exc:
            raise _artifact_error(exc) from exc
        if record.owner_account_id != caller.account_id and caller.account.permission_level < 1000:
            raise _error(status.HTTP_404_NOT_FOUND, "artifact_not_found", "制品不存在")
        await _authorize_artifact(runtime, policies, record, caller)
        await artifacts.revoke(
            artifact_id,
            account_id=caller.account_id,
            allow_non_owner=caller.account.permission_level >= 1000,
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.agent.artifact.revoke",
            resource_type="agent_artifact",
            resource_id=artifact_id,
        )

    return router


async def _owned_action(store: AgentStore, action_id: str, caller: WebUiPrincipal) -> AgentAction:
    try:
        return await store.get_action(action_id, account_id=caller.account_id)
    except AgentStoreError as exc:
        raise _store_error(exc) from exc


async def _authorize_action(
    runtime: Any,
    policies: KnowledgePolicyStore,
    action: AgentAction,
    caller: WebUiPrincipal,
    *,
    verify_target: bool,
) -> None:
    if caller.account.must_change_password and action.tool_name != "change_own_password":
        raise _error(status.HTTP_403_FORBIDDEN, "password_change_required", "必须先修改密码")
    missing = sorted(set(action.required_permissions) - set(caller.permissions))
    if missing:
        raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", f"缺少权限：{', '.join(missing)}")
    if action.tool_name == "create_knowledge_user" and caller.account.permission_level < 1000:
        raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "需要超级管理员权限")
    target = action.target
    if target is None and action.tool_name != "create_knowledge_user":
        workspace_id = action.canonical_arguments.get("workspace_id")
        user_id = action.canonical_arguments.get("user_id")
        permission = action.required_permissions[0] if action.required_permissions else None
        if isinstance(workspace_id, str) and permission:
            target = ActionTarget(resource_type="workspace", resource_id=workspace_id)
        elif isinstance(user_id, str) and permission:
            target = ActionTarget(resource_type="user", resource_id=user_id)
    if target is None:
        return
    permission = action.required_permissions[0] if action.required_permissions else ""
    if target.resource_type == "workspace":
        workspace = await runtime.metadata.get_workspace(target.resource_id)
        if workspace is None:
            raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "知识库不存在")
        permissions = set(caller.permissions)
        user_allowed = await policies.allows_user(
            caller.account,
            permissions,
            user_id=workspace.user_id,
            action="webui.user.read",
            permission="webui.user.read",
        )
        allowed = user_allowed and await policies.allows_workspace(
            caller.account,
            permissions,
            workspace_id=workspace.workspace_id,
            action=permission,
            permission=permission,
            user_id=workspace.user_id,
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "知识库不存在")
        if verify_target and target.display_name and workspace.workspace_name != target.display_name:
            raise _error(status.HTTP_409_CONFLICT, "stale_action_target", "目标名称已变化，请重新发起操作")
    elif target.resource_type == "user":
        users = {item.user_id: item for item in (await runtime.metadata.list_users()).users}
        user = users.get(target.resource_id)
        allowed = user is not None and await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=target.resource_id,
            action=permission,
            permission=permission,
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "知识域不存在")
        if verify_target and target.display_name and user.user_name != target.display_name:
            raise _error(status.HTTP_409_CONFLICT, "stale_action_target", "目标名称已变化，请重新发起操作")


async def _execute_confirmed(
    request: Request,
    caller: WebUiPrincipal,
    action: AgentAction,
    store: AgentStore,
    executor: ConfirmedActionExecutor,
    *,
    secure_values: Mapping[str, str] | None = None,
    upload: tuple[str, str, bytes] | None = None,
) -> dict[str, Any]:
    if not request.cookies.get(DEFAULT_COOKIE_NAME):
        raise _error(status.HTTP_401_UNAUTHORIZED, "session_required", "需要登录")
    try:
        executing = await store.start_action(action.action_id, account_id=caller.account_id)
        result = await executor.execute(
            executing,
            app=request.app,
            session_cookies=request.cookies,
            secure_values=secure_values,
            upload=upload,
        )
        summary = _safe_result_summary(result)
        try:
            succeeded = await store.succeed_action(
                action.action_id,
                account_id=caller.account_id,
                result_summary=summary,
            )
        except ActionPayloadError:
            # 业务接口已经成功时，结果摘要中的扩展字段不应阻止 Action 进入成功终态。
            succeeded = await store.succeed_action(
                action.action_id,
                account_id=caller.account_id,
                result_summary={"message": "操作已完成，详细结果未保存。"},
            )
        task = None
        if isinstance(result.get("task_id"), str):
            task = {
                "task_id": result["task_id"],
                "status": result.get("status", "queued"),
                "label": "后台任务",
            }
        return action_response(succeeded, task=task)
    except AgentExecutionError as exc:
        failed = await store.fail_action(
            action.action_id,
            account_id=caller.account_id,
            error=str(exc),
            result_summary={"code": exc.code},
        )
        return action_response(failed)
    except AgentStoreError as exc:
        raise _store_error(exc) from exc


def _safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """限制结果摘要大小，并排除任何可能的凭据字段。"""

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: sanitize(nested)
                for key, nested in value.items()
                if not any(marker in key.casefold() for marker in ("password", "token", "cookie"))
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value[:500]]
        if isinstance(value, str):
            return value[:2_000]
        return value

    return sanitize(result)


async def _authorize_artifact(
    runtime: Any,
    policies: KnowledgePolicyStore,
    artifact: Any,
    caller: WebUiPrincipal,
) -> None:
    missing = sorted(set(artifact.required_permissions) - set(caller.permissions))
    if missing:
        raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", f"缺少权限：{', '.join(missing)}")
    for requirement in artifact.resource_requirements:
        if requirement.resource_type == "workspace":
            workspace = await runtime.metadata.get_workspace(requirement.resource_id)
            if workspace is None:
                raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "制品关联资源已不存在")
            permissions = set(caller.permissions)
            user_allowed = await policies.allows_user(
                caller.account,
                permissions,
                user_id=workspace.user_id,
                action="webui.user.read",
                permission="webui.user.read",
            )
            allowed = user_allowed and await policies.allows_workspace(
                caller.account,
                permissions,
                workspace_id=requirement.resource_id,
                action=requirement.required_permission,
                permission=requirement.required_permission,
                user_id=workspace.user_id,
            )
        elif requirement.resource_type == "user":
            allowed = await policies.allows_user(
                caller.account,
                set(caller.permissions),
                user_id=requirement.resource_id,
                action=requirement.required_permission,
                permission=requirement.required_permission,
            )
        else:
            allowed = False
        if not allowed:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "当前账号不再具备制品资源权限")


def _store_error(exc: AgentStoreError) -> HTTPException:
    code = getattr(exc, "code", "agent_store_error")
    if isinstance(exc, (ActionNotFoundError, ActionOwnershipError)):
        return _error(status.HTTP_404_NOT_FOUND, "action_not_found", "操作不存在")
    if isinstance(exc, InvalidConfirmationError):
        return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, code, "确认文字与目标名称不一致")
    if isinstance(exc, ActionStateError):
        return _error(status.HTTP_409_CONFLICT, code, "操作状态不允许当前请求")
    return _error(status.HTTP_400_BAD_REQUEST, code, str(exc))


def _artifact_error(exc: Exception) -> HTTPException:
    code = "artifact_unavailable" if isinstance(exc, ArtifactUnavailableError) else "artifact_not_found"
    status_code = status.HTTP_410_GONE if isinstance(exc, ArtifactUnavailableError) else status.HTTP_404_NOT_FOUND
    return _error(status_code, code, "制品已过期、已撤销或不存在")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
