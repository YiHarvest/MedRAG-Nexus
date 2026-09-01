"""WebUI Agent 的能力网关与已确认操作执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx

from medrag_nexus.webui.router import DEFAULT_COOKIE_NAME

from .artifacts import ArtifactService
from .context import AgentContext
from .exports import export_answer_to_word
from .models import (
    AgentAction,
    AgentArtifactResponse,
    AnswerExportContent,
    ArtifactResourceRequirement,
)
from .store import AgentStore


class AgentExecutionError(RuntimeError):
    """表示既有 WebUI 业务接口拒绝了 Agent 操作。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def action_response(action: AgentAction, **extra: Any) -> dict[str, Any]:
    """生成前端可消费且不包含敏感输入的 Action 响应。"""

    return {
        "action_id": action.action_id,
        "tool_name": action.tool_name,
        "risk_level": action.risk_level,
        "confirmation_mode": action.confirmation_mode,
        "target": action.target.model_dump(mode="json") if action.target else None,
        "status": action.status,
        "result_summary": action.result_summary,
        "error": action.error,
        "expires_at": action.expires_at.isoformat(),
        **extra,
    }


def secure_input(action_id: str, tool_name: str) -> dict[str, Any]:
    """返回密码安全表单说明，实际密码值不会进入此结构。"""

    if tool_name == "change_own_password":
        fields = [
            {"name": "current_password", "label": "当前密码", "min_length": 1, "autocomplete": "current-password"},
            {"name": "new_password", "label": "新密码", "min_length": 3, "autocomplete": "new-password"},
        ]
        title = "修改密码"
    else:
        fields = [
            {"name": "new_password", "label": "新密码", "min_length": 3, "autocomplete": "new-password"}
        ]
        title = "设置账号密码"
    return {
        "action_id": action_id,
        "input_type": "password",
        "title": title,
        "description": "密码直接提交到服务端，不会发送给模型或写入聊天记录。",
        "fields": fields,
    }


def file_input(action_id: str) -> dict[str, Any]:
    """返回只能由浏览器文件选择器满足的输入要求。"""

    return {
        "action_id": action_id,
        "input_type": "file",
        "title": "选择本地文件",
        "description": "请选择要上传的本地文件；不接受网址或服务器路径。",
        "multiple": False,
        "max_files": 1,
    }


class AgentCapabilityService:
    """处理不应由模型直接携带字节或密码的能力。"""

    def __init__(
        self,
        *,
        action_store: AgentStore,
        artifacts: ArtifactService,
        action_ttl: timedelta,
        artifact_ttl: timedelta,
    ):
        self.action_store = action_store
        self.artifacts = artifacts
        self.action_ttl = action_ttl
        self.artifact_ttl = artifact_ttl

    async def invoke(
        self,
        capability: str,
        *,
        context: AgentContext,
        arguments: Mapping[str, Any],
    ) -> Any:
        if capability == "prepare_file_download":
            return await self._prepare_file_download(context, arguments)
        if capability == "export_answer_to_word":
            return await self._export_answer(context, arguments)
        if capability == "request_file_upload":
            return await self._request_input_action(context, "request_file_upload", arguments, "file")
        if capability == "change_own_password_secure_form":
            return await self._request_input_action(context, "change_own_password", {}, "password")
        if capability == "update_own_profile":
            account = await context.store.update_own_profile(
                account_id=context.principal.account_id,
                display_name=str(arguments["display_name"]),
            )
            return {
                "status": "succeeded",
                "message": "个人资料已更新。",
                "account": account.model_dump(mode="json", exclude={"password_hash"}),
            }
        if capability == "revoke_artifact":
            artifact = await self.artifacts.revoke(
                str(arguments["artifact_id"]),
                account_id=context.principal.account_id,
                allow_non_owner=context.principal.account.permission_level >= 1000,
            )
            return {
                "status": "succeeded",
                "artifact": AgentArtifactResponse.from_record(artifact).model_dump(mode="json"),
            }
        raise AgentExecutionError("unknown_capability", f"未知 Agent 能力：{capability}")

    async def _request_input_action(
        self,
        context: AgentContext,
        tool_name: str,
        arguments: Mapping[str, Any],
        input_type: str,
    ) -> dict[str, Any]:
        permission = (
            "webui.resource.file.add" if tool_name == "request_file_upload" else "webui.account.password.change_self"
        )
        action = await self.action_store.create_action(
            account_id=context.principal.account_id,
            conversation_id=context.conversation_id,
            tool_name=tool_name,
            canonical_arguments=dict(arguments),
            required_permissions=(permission,),
            risk_level="write" if input_type == "file" else "sensitive",
            ttl=self.action_ttl,
        )
        return {"status": "confirmation_required", "action": action_response(action)}

    async def _prepare_file_download(
        self,
        context: AgentContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        file_id = str(arguments["file_id"])
        resource = await context.runtime.metadata.get_file(workspace_id, file_id)
        if resource is None or resource.file_name is None:
            raise AgentExecutionError("file_not_found", "文件不存在", 404)
        source = context.runtime.artifacts.raw_file_path(resource)
        content = await asyncio.to_thread(source.read_bytes)
        artifact = await self.artifacts.create(
            owner_account_id=context.principal.account_id,
            file_name=resource.file_name,
            mime_type=resource.mime_type or "application/octet-stream",
            content=content,
            conversation_id=context.conversation_id,
            required_permissions=("webui.resource.file.download",),
            resource_requirements=(
                ArtifactResourceRequirement(
                    resource_type="workspace",
                    resource_id=workspace_id,
                    required_permission="webui.resource.file.download",
                ),
            ),
            ttl=self.artifact_ttl,
        )
        return {"status": "succeeded", "artifact": AgentArtifactResponse.from_record(artifact).model_dump(mode="json")}

    async def _export_answer(self, context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
        raw_sources = arguments.get("sources") if isinstance(arguments.get("sources"), list) else []
        sources: list[dict[str, Any]] = []
        resource_requirements: dict[str, ArtifactResourceRequirement] = {}
        remembered_resources = context.metadata.get("used_resources", {})
        if isinstance(remembered_resources, Mapping):
            for item in remembered_resources.values():
                try:
                    requirement = ArtifactResourceRequirement.model_validate(item)
                except Exception:
                    continue
                resource_requirements[
                    f"{requirement.resource_type}:{requirement.resource_id}:{requirement.required_permission}"
                ] = requirement
        for item in raw_sources[:100]:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or item.get("file_name") or "引用来源")[:256]
            sources.append(
                {
                    "title": title,
                    "reference": str(item.get("reference") or item.get("workspace_name") or "")[:512] or None,
                    "excerpt": str(item.get("excerpt") or "")[:2_000] or None,
                }
            )
            workspace_id = item.get("workspace_id")
            if isinstance(workspace_id, str) and workspace_id:
                await context.require_workspace(workspace_id, "webui.workspace.read")
                resource_requirements[f"workspace:{workspace_id}:webui.workspace.read"] = ArtifactResourceRequirement(
                    resource_type="workspace",
                    resource_id=workspace_id,
                    required_permission="webui.workspace.read",
                )
        content = AnswerExportContent(
            question=str(arguments.get("question") or ""),
            answer=str(arguments["answer"]),
            sources=sources,
            generated_by=context.principal.account.display_name,
        )
        required_permissions = {"webui.agent.export"}
        remembered_permissions = context.metadata.get("used_permissions", set())
        if isinstance(remembered_permissions, (set, list, tuple)):
            required_permissions.update(
                permission
                for permission in remembered_permissions
                if isinstance(permission, str) and permission in context.principal.permissions
            )
        requested_permissions = arguments.get("required_permissions", [])
        if isinstance(requested_permissions, list):
            for permission in requested_permissions:
                if not isinstance(permission, str) or permission not in context.principal.permissions:
                    raise AgentExecutionError("permission_denied", "导出包含当前账号没有的权限要求", 403)
                required_permissions.add(permission)
        artifact = await export_answer_to_word(
            self.artifacts,
            owner_account_id=context.principal.account_id,
            content=content,
            conversation_id=context.conversation_id,
            required_permissions=tuple(sorted(required_permissions)),
            resource_requirements=tuple(resource_requirements.values()),
            ttl=self.artifact_ttl,
        )
        return {"status": "succeeded", "artifact": AgentArtifactResponse.from_record(artifact).model_dump(mode="json")}


class ConfirmedActionExecutor:
    """通过应用内 ASGI 调用复用既有 WebUI 业务接口及其校验。"""

    def __init__(self, *, cookie_name: str = DEFAULT_COOKIE_NAME):
        self.cookie_name = cookie_name

    async def execute(
        self,
        action: AgentAction,
        *,
        app: Any,
        session_cookies: Mapping[str, str],
        secure_values: Mapping[str, str] | None = None,
        upload: tuple[str, str, bytes] | None = None,
    ) -> dict[str, Any]:
        method, path, payload = self._request_spec(action, secure_values)
        transport = httpx.ASGITransport(app=app)
        cookies = dict(session_cookies)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.internal", cookies=cookies) as client:
            if upload is not None:
                file_name, mime_type, content = upload
                response = await client.request(
                    method,
                    path,
                    data={"type": "file"},
                    files={"file": (file_name, content, mime_type)},
                )
            elif action.tool_name == "add_text_resource":
                response = await client.request(method, path, data=payload)
            else:
                response = await client.request(method, path, json=payload)
        data = self._response_data(response)
        if response.status_code >= 400:
            code, message = self._error_detail(data)
            raise AgentExecutionError(code, message, response.status_code)
        return data

    @staticmethod
    def _response_data(response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {"status": "ok"}
        value = response.json()
        return value if isinstance(value, dict) else {"result": value}

    @staticmethod
    def _error_detail(data: dict[str, Any]) -> tuple[str, str]:
        detail = data.get("detail")
        if isinstance(detail, dict):
            return str(detail.get("code") or "action_failed"), str(detail.get("message") or "操作失败")
        return "action_failed", str(detail or "操作失败")

    @staticmethod
    def _request_spec(
        action: AgentAction,
        secure_values: Mapping[str, str] | None,
    ) -> tuple[str, str, dict[str, Any]]:
        args = dict(action.canonical_arguments)
        name = action.tool_name
        if name == "request_file_upload":
            workspace_id = quote(str(args["workspace_id"]), safe="")
            return "POST", f"/api/v1/workspaces/{workspace_id}/resources", {}
        if name == "change_own_password":
            return "POST", "/api/v1/account/password", dict(secure_values or {})
        if name == "create_account":
            payload = {**args, "password": str((secure_values or {}).get("new_password", ""))}
            return "POST", "/api/v1/accounts", payload
        if name == "reset_account_password":
            account_id = quote(str(args.pop("account_id")), safe="")
            payload = {**args, "new_password": str((secure_values or {}).get("new_password", ""))}
            return "POST", f"/api/v1/accounts/{account_id}/password/reset", payload
        path_specs: dict[str, tuple[str, str, str | None]] = {
            "create_knowledge_user": ("POST", "/api/v1/users", None),
            "rename_knowledge_user": ("PATCH", "/api/v1/users/{user_id}", "user_id"),
            "create_workspace": ("POST", "/api/v1/workspaces", None),
            "rename_workspace": ("PATCH", "/api/v1/workspaces/{workspace_id}", "workspace_id"),
            "add_text_resource": ("POST", "/api/v1/workspaces/{workspace_id}/resources", "workspace_id"),
            "revoke_artifact": ("DELETE", "/api/v1/agent/artifacts/{artifact_id}", "artifact_id"),
            "update_account": ("PATCH", "/api/v1/accounts/{account_id}", "account_id"),
            "bind_account_to_user": ("PUT", "/api/v1/accounts/{account_id}/binding", "account_id"),
            "create_permission_group": ("POST", "/api/v1/permission-groups", None),
            "update_permission_group": ("PATCH", "/api/v1/permission-groups/{group_key}", "group_key"),
            "delete_permission_group": ("DELETE", "/api/v1/permission-groups/{group_key}", "group_key"),
            "leave_own_permission_group": (
                "DELETE",
                "/api/v1/account/permission-groups/{group_key}",
                "group_key",
            ),
            "update_user_policy": ("PUT", "/api/v1/users/{user_id}/policy", "user_id"),
            "update_workspace_policy": ("PUT", "/api/v1/workspaces/{workspace_id}/policy", "workspace_id"),
            "replace_user_bindings": ("PUT", "/api/v1/users/{user_id}/bindings", "user_id"),
            "replace_workspace_bindings": ("PUT", "/api/v1/workspaces/{workspace_id}/bindings", "workspace_id"),
            "delete_file": ("DELETE", "/api/v1/workspaces/{workspace_id}/files/{file_id}", None),
            "delete_text_resource": ("DELETE", "/api/v1/workspaces/{workspace_id}/strings/{content_hash}", None),
            "delete_workspace": ("DELETE", "/api/v1/workspaces/{workspace_id}", None),
            "delete_knowledge_user": ("DELETE", "/api/v1/users/{user_id}", None),
        }
        if name not in path_specs:
            raise AgentExecutionError("unsupported_action", f"不支持执行操作：{name}")
        method, template, removed_key = path_specs[name]
        path = template.format(**{key: quote(str(value), safe="") for key, value in args.items()})
        if removed_key:
            args.pop(removed_key, None)
        if name == "add_text_resource":
            args = {"type": "str", "content": str(args["content"])}
        if name == "delete_workspace":
            path = f"{path}?confirm_name={quote(str(args['workspace_name']), safe='')}"
        if name == "delete_knowledge_user":
            path = f"{path}?confirm_name={quote(str(args['user_name']), safe='')}"
        if name in {"delete_file", "delete_workspace", "delete_knowledge_user"}:
            args = {}
        if name == "delete_text_resource":
            args = {}
        return method, path, args
