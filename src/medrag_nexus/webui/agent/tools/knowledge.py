"""知识域与知识库 Agent 工具。

普通且可逆的写操作复用现有运行时、存储和策略服务；删除操作只创建持久化确认意图，绝不在这里执行。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from medrag_nexus.core.ids import normalize_workspace_name
from medrag_nexus.core.models import AddRequest, StringSource, UserCreateRequest, WorkspaceRecord
from medrag_nexus.services.files import FileService

from ..context import AgentAuthorizationError, AgentContext, _jsonable
from ..registry import ToolSpec, object_schema
from .read import _required_string, _string


async def create_knowledge_user(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    user_name = _required_string(arguments, "user_name")
    created = await FileService(context.runtime).create_user(UserCreateRequest(user_id=user_id, user_name=user_name))
    try:
        await context.policies.set_user_policy(
            user_id,
            read_min_level=0,
            workspace_create_min_level=0,
            actor_account_id=context.principal.account_id,
        )
        await context.policies.ensure_resource_acl("user", user_id)
        await context.policies.grant_user_creator_access(context.principal.account_id, user_id)
    except Exception:
        await context.runtime.metadata.delete_user(user_id)
        await context.policies.delete_user_policy_data(user_id)
        raise
    await context.store.record_audit(
        actor_account_id=context.principal.account_id,
        action="webui.user.create",
        resource_type="user",
        resource_id=user_id,
        after=_jsonable(created),
    )
    return _jsonable(created)


async def rename_knowledge_user(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    user_name = _required_string(arguments, "user_name")
    await context.require_user(user_id, "webui.user.rename")
    before = next(
        (user for user in (await context.runtime.metadata.list_users()).users if user.user_id == user_id), None
    )
    if before is None:
        raise AgentAuthorizationError("user_not_found", "knowledge user does not exist")
    renamed = await context.runtime.metadata.rename_user(user_id, user_name)
    await context.store.record_audit(
        actor_account_id=context.principal.account_id,
        action="webui.user.rename",
        resource_type="user",
        resource_id=user_id,
        before={"user_name": before.user_name},
        after={"user_name": renamed.user_name},
    )
    return _jsonable(renamed)


async def create_workspace(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    workspace_name = normalize_workspace_name(_required_string(arguments, "workspace_name"))
    await context.require_user(user_id, "webui.workspace.create")
    workspace = WorkspaceRecord(
        workspace_id=f"workspace_{uuid4()}",
        user_id=user_id,
        workspace_name=workspace_name,
    )
    try:
        await context.runtime.metadata.create_workspace(workspace)
        await context.policies.set_workspace_policy(
            workspace.workspace_id,
            read_min_level=0,
            cud_min_level=0,
            actor_account_id=context.principal.account_id,
            creating=True,
        )
        await context.policies.ensure_resource_acl("workspace", workspace.workspace_id, user_id=user_id)
        await context.policies.grant_workspace_creator_access(context.principal.account_id, workspace.workspace_id)
        await context.policies.mark_lifecycle(
            workspace.workspace_id,
            "active",
            actor_account_id=context.principal.account_id,
        )
        await context.runtime.elasticsearch.mirror_workspace(workspace)
    except sqlite3.IntegrityError as exc:
        raise ValueError("workspace name already exists") from exc
    except Exception:
        if await context.runtime.metadata.get_workspace(workspace.workspace_id) is not None:
            await context.runtime.metadata.delete_workspace(workspace.workspace_id)
        await context.policies.delete_workspace_policy_data(workspace.workspace_id)
        raise
    await context.store.record_audit(
        actor_account_id=context.principal.account_id,
        action="webui.workspace.create",
        resource_type="workspace",
        resource_id=workspace.workspace_id,
        after=_jsonable(workspace),
    )
    return _jsonable(workspace)


async def request_create_knowledge_user(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    user_name = _required_string(arguments, "user_name")
    return await context.request_action(
        tool_name="create_knowledge_user",
        arguments={"user_id": user_id, "user_name": user_name},
        required_permissions=("webui.user.create",),
        risk_level="write",
        confirmation_mode="click",
    )


async def request_create_workspace(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    workspace_name = normalize_workspace_name(_required_string(arguments, "workspace_name"))
    await context.require_user(user_id, "webui.workspace.create")
    return await context.request_action(
        tool_name="create_workspace",
        arguments={"user_id": user_id, "workspace_name": workspace_name},
        required_permissions=("webui.workspace.create",),
        risk_level="write",
        target={"resource_type": "user", "resource_id": user_id},
        confirmation_mode="click",
    )


async def request_rename_knowledge_user(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    user_id = _required_string(arguments, "user_id")
    user_name = _required_string(arguments, "user_name")
    await context.require_user(user_id, "webui.user.rename")
    return await context.request_action(
        tool_name="rename_knowledge_user",
        arguments={"user_id": user_id, "user_name": user_name},
        required_permissions=("webui.user.rename",),
        risk_level="write",
        target={"resource_type": "user", "resource_id": user_id},
        confirmation_mode="click",
    )


async def request_rename_workspace(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    workspace_name = normalize_workspace_name(_required_string(arguments, "workspace_name"))
    await context.require_workspace(workspace_id, "webui.workspace.rename")
    return await context.request_action(
        tool_name="rename_workspace",
        arguments={"workspace_id": workspace_id, "workspace_name": workspace_name},
        required_permissions=("webui.workspace.rename",),
        risk_level="write",
        target={"resource_type": "workspace", "resource_id": workspace_id},
        confirmation_mode="click",
    )


async def request_add_text_resource(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    content = _required_string(arguments, "content")
    await context.require_workspace(workspace_id, "webui.resource.text.add")
    return await context.request_action(
        tool_name="add_text_resource",
        arguments={"workspace_id": workspace_id, "content": content},
        required_permissions=("webui.resource.text.add",),
        risk_level="write",
        target={"resource_type": "workspace", "resource_id": workspace_id},
        confirmation_mode="click",
    )


async def rename_workspace(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    workspace_name = normalize_workspace_name(_required_string(arguments, "workspace_name"))
    workspace = await context.require_workspace(workspace_id, "webui.workspace.rename")
    async with context.runtime.tasks.workspace_lock(workspace.user_id, workspace_id):
        if await context.runtime.metadata.workspace_has_active_tasks(workspace_id):
            raise ValueError("workspace has active tasks")
        renamed = await context.runtime.metadata.rename_workspace(workspace_id, workspace_name)
        try:
            await context.runtime.elasticsearch.rename_workspace(renamed)
        except Exception:
            restored = await context.runtime.metadata.rename_workspace(workspace_id, workspace.workspace_name)
            await context.runtime.elasticsearch.rename_workspace(restored)
            raise
    await context.store.record_audit(
        actor_account_id=context.principal.account_id,
        action="webui.workspace.rename",
        resource_type="workspace",
        resource_id=workspace_id,
        before={"workspace_name": workspace.workspace_name},
        after={"workspace_name": renamed.workspace_name},
    )
    return _jsonable(renamed)


async def add_text_resource(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    content = _required_string(arguments, "content")
    workspace = await context.require_workspace(workspace_id, "webui.resource.text.add")
    accepted = await FileService(context.runtime).submit_add(
        AddRequest(
            user_id=workspace.user_id,
            workspace_id=workspace_id,
            workspace_name=workspace.workspace_name,
            source=StringSource(content=content),
        )
    )
    await context.store.record_audit(
        actor_account_id=context.principal.account_id,
        action="webui.resource.text.add",
        resource_type="workspace",
        resource_id=workspace_id,
        after={"task_id": accepted.task_id, "size_bytes": len(content.encode("utf-8"))},
    )
    return _jsonable(accepted)


async def request_file_upload(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    workspace = await context.require_workspace(workspace_id, "webui.resource.file.add")
    return await context.invoke_capability(
        "request_file_upload",
        {"workspace_id": workspace_id, "workspace_name": workspace.workspace_name, "user_id": workspace.user_id},
    )


def _deferred(
    tool_name: str,
    required_permissions: tuple[str, ...],
    *,
    resource_kind: str,
    resource_key: str,
    permission: str,
    confirmation_mode: str = "click",
    display_name_key: str | None = None,
):
    async def handler(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
        resource_id = _required_string(arguments, resource_key)
        if resource_kind == "workspace":
            await context.require_workspace(resource_id, permission)
        elif resource_kind == "user":
            await context.require_user(resource_id, permission)
        target = {"resource_type": resource_kind, "resource_id": resource_id}
        if display_name_key is not None:
            target["display_name"] = _required_string(arguments, display_name_key)
        return await context.request_action(
            tool_name=tool_name,
            arguments=arguments,
            required_permissions=required_permissions,
            risk_level="destructive",
            target=target,
            confirmation_mode=confirmation_mode,
        )

    return handler


def knowledge_tool_specs() -> tuple[ToolSpec, ...]:
    workspace_id = _string("知识库 ID")
    user_id = _string("知识域 ID")
    return (
        ToolSpec(
            "create_knowledge_user",
            "创建知识域；只创建点击确认单，用户确认后才会执行。",
            object_schema({"user_id": user_id, "user_name": _string("知识域名称")}, required=("user_id", "user_name")),
            request_create_knowledge_user,
            ("webui.user.create",),
            superadmin_guard=True,
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "rename_knowledge_user",
            "重命名获授权知识域；只创建点击确认单。",
            object_schema({"user_id": user_id, "user_name": _string("新名称")}, required=("user_id", "user_name")),
            request_rename_knowledge_user,
            ("webui.user.rename",),
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "create_workspace",
            "在获授权知识域下创建知识库；只创建点击确认单，用户确认后才会执行。",
            object_schema(
                {"user_id": user_id, "workspace_name": _string("知识库名称")}, required=("user_id", "workspace_name")
            ),
            request_create_workspace,
            ("webui.workspace.create",),
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "rename_workspace",
            "重命名获授权知识库；只创建点击确认单。",
            object_schema(
                {"workspace_id": workspace_id, "workspace_name": _string("新名称")},
                required=("workspace_id", "workspace_name"),
            ),
            request_rename_workspace,
            ("webui.workspace.rename",),
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "add_text_resource",
            "向获授权知识库添加文本；只创建点击确认单，确认后提交异步任务。",
            object_schema(
                {"workspace_id": workspace_id, "content": _string("文本内容", max_length=1_000_000)},
                required=("workspace_id", "content"),
            ),
            request_add_text_resource,
            ("webui.resource.text.add",),
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "request_file_upload",
            "请求浏览器展示本地文件选择器；模型不接收文件字节或路径。",
            object_schema({"workspace_id": workspace_id}, required=("workspace_id",)),
            request_file_upload,
            ("webui.resource.file.add",),
            risk_level="write",
            input_mode="file_picker",
        ),
        ToolSpec(
            "delete_file",
            "删除文件资源；仅创建二次确认单。",
            object_schema(
                {"workspace_id": workspace_id, "file_id": _string("文件 ID"), "file_name": _string("文件名")},
                required=("workspace_id", "file_id", "file_name"),
            ),
            _deferred(
                "delete_file",
                ("webui.resource.file.delete",),
                resource_kind="workspace",
                resource_key="workspace_id",
                permission="webui.resource.file.delete",
            ),
            ("webui.resource.file.delete",),
            risk_level="destructive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "delete_text_resource",
            "删除文本资源；仅创建二次确认单。",
            object_schema(
                {"workspace_id": workspace_id, "content_hash": _string("文本内容哈希")},
                required=("workspace_id", "content_hash"),
            ),
            _deferred(
                "delete_text_resource",
                ("webui.resource.text.delete",),
                resource_kind="workspace",
                resource_key="workspace_id",
                permission="webui.resource.text.delete",
            ),
            ("webui.resource.text.delete",),
            risk_level="destructive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "delete_workspace",
            "删除知识库；仅创建需输入目标名称的二次确认单。",
            object_schema(
                {"workspace_id": workspace_id, "workspace_name": _string("知识库名称")},
                required=("workspace_id", "workspace_name"),
            ),
            _deferred(
                "delete_workspace",
                ("webui.workspace.delete",),
                resource_kind="workspace",
                resource_key="workspace_id",
                permission="webui.workspace.delete",
                confirmation_mode="typed_text",
                display_name_key="workspace_name",
            ),
            ("webui.workspace.delete",),
            risk_level="destructive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "delete_knowledge_user",
            "删除知识域及全部知识库；仅创建需输入目标名称的二次确认单。",
            object_schema({"user_id": user_id, "user_name": _string("知识域名称")}, required=("user_id", "user_name")),
            _deferred(
                "delete_knowledge_user",
                ("webui.user.delete",),
                resource_kind="user",
                resource_key="user_id",
                permission="webui.user.delete",
                confirmation_mode="typed_text",
                display_name_key="user_name",
            ),
            ("webui.user.delete",),
            superadmin_guard=True,
            risk_level="destructive",
            input_mode="confirmation",
        ),
    )
