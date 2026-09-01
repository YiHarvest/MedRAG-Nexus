"""按权限过滤的项目 WebUI 专用 BFF。"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import asdict
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field, StringConstraints
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, StreamingResponse

from jd_knowledge.core.ids import normalize_workspace_name
from jd_knowledge.core.models import (
    AddRequest,
    APIModel,
    ChatMessage,
    ChatRequest,
    DeleteFileRequest,
    DeleteStringRequest,
    FileListResponse,
    FileSource,
    HealthResponse,
    Identifier,
    RetrievalRequest,
    RetrievalResponse,
    StringSource,
    TaskAccepted,
    TaskResponse,
    WorkspaceRecord,
)
from jd_knowledge.services.chat import stream_chat
from jd_knowledge.services.files import FileService
from jd_knowledge.services.health import dependency_health
from jd_knowledge.services.retrieval import retrieve
from jd_knowledge.services.runtime import Runtime

from .agent.context import AgentContext
from .models import AccountRecord
from .policy_store import (
    USER_POLICY_ACTIONS,
    WORKSPACE_POLICY_ACTIONS,
    InvalidPolicyBindingError,
    KnowledgePolicyStore,
    PolicyBinding,
)
from .router import DEFAULT_COOKIE_NAME, WebUiPrincipal, create_principal_dependency
from .store import AccountBindingError, WebUiStore

WorkspaceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class WorkspaceCapabilities(APIModel):
    can_read: bool
    can_add_file: bool
    can_download_file: bool
    can_add_text: bool
    can_delete_file: bool
    can_delete_text: bool
    can_add_resource: bool
    can_delete_resource: bool
    can_rename: bool
    can_delete_workspace: bool
    can_manage_policy: bool


class AccessibleWorkspace(APIModel):
    workspace_id: str
    workspace_name: str
    user_id: str
    resource_count: int
    file_count: int
    str_count: int
    total_size_bytes: int
    created_at: str
    modified_at: str
    read_min_level: int
    cud_min_level: int
    policy_version: int
    capabilities: WorkspaceCapabilities


class AccessibleUser(APIModel):
    user_id: str
    user_name: str
    workspace_count: int = 0
    resource_count: int = 0
    file_count: int = 0
    str_count: int = 0
    total_size_bytes: int = 0
    read_min_level: int
    workspace_create_min_level: int
    policy_version: int
    can_create_workspace: bool
    can_manage_policy: bool
    can_rename: bool
    can_delete: bool


class AccessibleKnowledgeResponse(APIModel):
    users: list[AccessibleUser]
    workspaces: list[AccessibleWorkspace]


class CreateWorkspaceRequest(APIModel):
    user_id: Identifier
    workspace_name: WorkspaceName
    read_min_level: int = Field(default=0, ge=0)
    cud_min_level: int = Field(default=0, ge=0)


class CreateKnowledgeUserRequest(APIModel):
    user_id: Identifier | None = None
    user_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    read_min_level: int = Field(default=0, ge=0)
    workspace_create_min_level: int = Field(default=0, ge=0)
    bind_account_id: str | None = Field(default=None, min_length=1, max_length=128)


class AccountKnowledgeBindingRequest(APIModel):
    user_id: Identifier | None = None
    bound: bool = True


class AccountKnowledgeBindingsRequest(APIModel):
    user_ids: list[Identifier] = Field(default_factory=list, max_length=500)


class RenameKnowledgeUserRequest(APIModel):
    user_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class RenameWorkspaceRequest(APIModel):
    workspace_name: WorkspaceName


class WorkspacePolicyRequest(APIModel):
    read_min_level: int = Field(ge=0)
    cud_min_level: int = Field(ge=0)


class UserPolicyRequest(APIModel):
    read_min_level: int = Field(ge=0)
    workspace_create_min_level: int = Field(ge=0)


class PolicyBindingItem(APIModel):
    principal_type: Literal["account", "group"]
    principal_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    effect: Literal["allow", "deny"]
    immutable: bool = False
    managed_by: str | None = None


class PolicyBindingInput(APIModel):
    principal_type: Literal["account", "group"]
    principal_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    effect: Literal["allow", "deny"]


class PolicyBindingsUpdateRequest(APIModel):
    action: str = Field(min_length=1, max_length=128)
    bindings: list[PolicyBindingInput] = Field(default_factory=list, max_length=500)


class PolicyBindingsResponse(APIModel):
    resource_type: Literal["user", "workspace"]
    resource_id: str
    bindings: dict[str, list[PolicyBindingItem]]


class WebUiRetrievalRequest(APIModel):
    workspace_id: str
    query: str = Field(min_length=1, max_length=10_000)
    top_k: int | None = Field(default=None, ge=1)


class WebUiChatRequest(APIModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    top_k: int = Field(default=8, ge=1, le=20)
    conversation_id: str | None = Field(default=None, max_length=128)


class FileDetailResponse(APIModel):
    workspace_id: str
    file_id: str
    file_name: str
    mime_type: str | None = None
    content_hash: str
    markdown_hash: str | None = None
    size_bytes: int
    parser: str
    degraded: bool
    chunk_count: int
    created_at: str
    modified_at: str
    downloadable: bool


class DeleteWorkspaceResponse(APIModel):
    status: Literal["deleted", "cleanup_pending"] = "deleted"
    workspace_id: str


class DeleteKnowledgeUserResponse(APIModel):
    status: Literal["deleted", "cleanup_pending"] = "deleted"
    user_id: str
    deleted_workspace_count: int = 0


def create_knowledge_router(
    runtime: Runtime,
    webui_store: WebUiStore,
    policies: KnowledgePolicyStore,
    *,
    cookie_name: str = DEFAULT_COOKIE_NAME,
    agent_store: Any | None = None,
    agent_registry: Any | None = None,
    agent_capabilities: Any | None = None,
) -> APIRouter:
    principal_dependency = create_principal_dependency(webui_store, cookie_name=cookie_name)
    allowed_levels = frozenset(level.value for level in webui_store.registry.levels)
    router = APIRouter(prefix="/api/webui/v1", tags=["WebUI 知识权限"])
    principal_dependency_dep = Depends(principal_dependency)

    async def principal(
        value: WebUiPrincipal = principal_dependency_dep,
    ) -> WebUiPrincipal:
        if value.account.must_change_password:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "password_change_required",
                "password must be changed before using knowledge operations",
            )
        return value

    principal_dep = Depends(principal)

    def require_superadmin(caller: WebUiPrincipal, permission: str) -> None:
        if (
            caller.account.permission_level < 1000
            or permission not in caller.permissions
        ):
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "superadmin permission is required")

    async def require_workspace(
        caller: WebUiPrincipal,
        workspace_id: str,
        *,
        action: str,
        permission: str,
    ) -> WorkspaceRecord:
        workspace = await runtime.metadata.get_workspace(workspace_id)
        if workspace is None:
            raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "workspace does not exist")
        permissions = set(caller.permissions)
        user_allowed = await policies.allows_user(
            caller.account,
            permissions,
            user_id=workspace.user_id,
            action="webui.user.read",
            permission="webui.user.read",
        )
        workspace_allowed = user_allowed and await policies.allows_workspace(
            caller.account,
            permissions,
            workspace_id=workspace_id,
            action=action,
            permission=permission,
            user_id=workspace.user_id,
        )
        if not workspace_allowed:
            # 不向无权限调用者暴露隐藏 Workspace 是否真实存在。
            raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "workspace does not exist")
        return workspace

    async def require_user_policy_scope(caller: WebUiPrincipal, user_id: str) -> None:
        """要求调用者同时具备策略节点权限与目标 UserID 的可见范围。"""

        known_users = {user.user_id for user in (await runtime.metadata.list_users()).users}
        if user_id not in known_users:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        allowed = await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=user_id,
            action="webui.user.policy.manage",
            permission="webui.user.policy.manage",
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")

    def binding_response(
        resource_type: Literal["user", "workspace"],
        resource_id: str,
        values: dict[str, list[PolicyBinding]],
    ) -> PolicyBindingsResponse:
        return PolicyBindingsResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            bindings={
                action: [PolicyBindingItem(**asdict(binding)) for binding in bindings]
                for action, bindings in values.items()
            },
        )

    async def account_binding_scope(user_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """解析多知识域绑定对应的全部知识库，用于原子重建系统 ACL。"""

        known_users = {user.user_id for user in (await runtime.metadata.list_users()).users}
        unknown = sorted(set(user_ids) - known_users)
        if unknown:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        scope: dict[str, tuple[str, ...]] = {}
        for user_id in sorted(set(user_ids)):
            summaries = (await runtime.metadata.list_workspaces(user_id)).workspaces
            scope[user_id] = tuple(summary.workspace_id for summary in summaries)
        return scope

    async def replace_account_bindings(
        target: AccountRecord,
        user_ids: list[str],
        caller: WebUiPrincipal,
    ) -> AccountRecord:
        """保存普通账号多知识域绑定，并同步全部系统管理 ACL。"""

        if target.permission_level >= 1000:
            raise _error(
                status.HTTP_409_CONFLICT,
                "superadmin_binding_unnecessary",
                "超级管理员固定拥有全部知识域权限，不需要绑定知识域",
            )
        normalized = sorted(set(user_ids))
        scope = await account_binding_scope(normalized)
        before = list(target.bound_user_ids)
        try:
            updated = await webui_store.set_account_user_bindings(
                target.account_id,
                normalized,
                actor_account_id=caller.account_id,
            )
        except AccountBindingError as exc:
            raise _error(status.HTTP_409_CONFLICT, "account_binding_conflict", str(exc)) from exc
        try:
            await policies.replace_account_binding_access(target.account_id, scope)
            for user_id, workspace_ids in scope.items():
                await policies.ensure_resource_acl("user", user_id)
                for workspace_id in workspace_ids:
                    await policies.ensure_resource_acl("workspace", workspace_id, user_id=user_id)
        except Exception:
            await webui_store.set_account_user_bindings(
                target.account_id,
                before,
                actor_account_id=caller.account_id,
            )
            raise
        return updated

    async def validate_binding_targets(action: str, values: list[PolicyBindingInput]) -> None:
        """显式允许只能缩小资源范围，不能为目标主体凭空增加权限节点。"""

        group_permissions: dict[str, set[str]] | None = None
        for binding in values:
            if binding.effect != "allow":
                continue
            if binding.principal_type == "account":
                target = await webui_store.get_account(binding.principal_id)
                permissions = (
                    await webui_store.permission_keys(binding.principal_id) if target is not None else set()
                )
            else:
                if group_permissions is None:
                    group_permissions = {
                        group.key: set(group.permissions)
                        for group in await webui_store.list_permission_groups()
                    }
                permissions = group_permissions.get(binding.principal_id, set())
            if action not in permissions:
                subject = "用户" if binding.principal_type == "account" else "权限组"
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "ineffective_policy_binding",
                    f"所选{subject}本身没有此操作权限，不能建立无效授权",
                )

    async def workspace_item(caller: WebUiPrincipal, workspace: WorkspaceRecord) -> AccessibleWorkspace:
        policy = await policies.get_workspace_policy(workspace.workspace_id)
        permissions = set(caller.permissions)

        async def capability(node: str) -> bool:
            return await policies.allows_workspace(
                caller.account,
                permissions,
                workspace_id=workspace.workspace_id,
                action=node,
                permission=node,
                user_id=workspace.user_id,
            )

        can_add_file = await capability("webui.resource.file.add")
        can_download_file = await capability("webui.resource.file.download")
        can_add_text = await capability("webui.resource.text.add")
        can_delete_file = await capability("webui.resource.file.delete")
        can_delete_text = await capability("webui.resource.text.delete")
        capabilities = WorkspaceCapabilities(
            can_read=await capability("webui.workspace.read"),
            can_add_file=can_add_file,
            can_download_file=can_download_file,
            can_add_text=can_add_text,
            can_delete_file=can_delete_file,
            can_delete_text=can_delete_text,
            can_add_resource=can_add_file or can_add_text,
            can_delete_resource=can_delete_file or can_delete_text,
            can_rename=await capability("webui.workspace.rename"),
            can_delete_workspace=await capability("webui.workspace.delete"),
            can_manage_policy=await capability("webui.workspace.policy.manage"),
        )
        return AccessibleWorkspace(
            **workspace.model_dump(mode="json"),
            read_min_level=policy.read_min_level,
            cud_min_level=policy.cud_min_level,
            policy_version=policy.policy_version,
            capabilities=capabilities,
        )

    @router.post("/users", status_code=status.HTTP_201_CREATED)
    async def create_knowledge_user(
        payload: CreateKnowledgeUserRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        """创建与登录用户分离的知识域。"""

        if "webui.user.create" not in caller.permissions:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "knowledge domain creation is not allowed")
        _validate_thresholds(
            caller,
            payload.read_min_level,
            payload.workspace_create_min_level,
            allowed_levels=allowed_levels,
        )
        target_account = None
        if payload.bind_account_id is not None:
            if "webui.user.binding.manage" not in caller.permissions:
                raise _error(
                    status.HTTP_403_FORBIDDEN,
                    "permission_denied",
                    "default knowledge domain binding is not allowed",
                )
            target_account = await webui_store.get_account(payload.bind_account_id)
            if target_account is None:
                raise _error(status.HTTP_404_NOT_FOUND, "account_not_found", "account does not exist")
            if target_account.permission_level >= 1000:
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "superadmin_binding_unnecessary",
                    "超级管理员固定拥有全部知识域权限，不需要绑定知识域",
                )
        user_id = payload.user_id or f"user_{uuid4().hex}"
        created = await runtime.metadata.create_user(user_id, payload.user_name)
        if created is None:
            raise _error(status.HTTP_409_CONFLICT, "user_id_conflict", "knowledge user already exists")
        try:
            policy = await policies.set_user_policy(
                user_id,
                read_min_level=payload.read_min_level,
                workspace_create_min_level=payload.workspace_create_min_level,
                actor_account_id=caller.account_id,
            )
            await policies.ensure_resource_acl("user", user_id)
            await policies.grant_user_creator_access(caller.account_id, user_id)
            if target_account is not None:
                await replace_account_bindings(
                    target_account,
                    [*target_account.bound_user_ids, user_id],
                    caller,
                )
        except AccountBindingError as exc:
            await webui_store.record_audit(
                actor_account_id=caller.account_id,
                action="webui.user.create.binding_failed",
                resource_type="user",
                resource_id=user_id,
                after={"bind_account_id": payload.bind_account_id},
            )
            raise _error(status.HTTP_409_CONFLICT, "account_binding_conflict", str(exc)) from exc
        except Exception:
            # SQLiteStore 暂无删除空 UserID API；保留空用户并让审计暴露失败，避免直接操作公共表。
            await webui_store.record_audit(
                actor_account_id=caller.account_id,
                action="webui.user.create.failed",
                resource_type="user",
                resource_id=user_id,
                after={"user_name": payload.user_name},
            )
            raise
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.create",
            resource_type="user",
            resource_id=user_id,
            after={
                **created.model_dump(mode="json"),
                **asdict(policy),
                "bound_account_id": target_account.account_id if target_account else None,
            },
        )
        return {
            **created.model_dump(mode="json"),
            **asdict(policy),
            "bound_account_id": target_account.account_id if target_account else None,
        }

    @router.put("/accounts/{account_id}/binding")
    async def bind_account_to_knowledge_user(
        account_id: str,
        payload: AccountKnowledgeBindingRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        """增加或移除普通账号的一个知识域绑定。"""

        require_superadmin(caller, "webui.user.binding.manage")
        target = await webui_store.get_account(account_id)
        if target is None:
            raise _error(status.HTTP_404_NOT_FOUND, "account_not_found", "account does not exist")
        next_user_ids = set(target.bound_user_ids)
        if payload.user_id is None:
            next_user_ids.clear()
        elif payload.bound:
            next_user_ids.add(payload.user_id)
        else:
            next_user_ids.discard(payload.user_id)
        updated = await replace_account_bindings(target, sorted(next_user_ids), caller)
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.account.knowledge_binding.update",
            resource_type="account",
            resource_id=account_id,
            before={"bound_user_ids": target.bound_user_ids},
            after={"bound_user_ids": updated.bound_user_ids},
        )
        return {
            "account_id": account_id,
            "bound_user_id": updated.bound_user_id,
            "bound_user_ids": updated.bound_user_ids,
        }

    @router.put("/accounts/{account_id}/bindings")
    async def replace_account_knowledge_bindings(
        account_id: str,
        payload: AccountKnowledgeBindingsRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        """替换普通账号的全部知识域绑定。"""

        require_superadmin(caller, "webui.user.binding.manage")
        target = await webui_store.get_account(account_id)
        if target is None:
            raise _error(status.HTTP_404_NOT_FOUND, "account_not_found", "account does not exist")
        updated = await replace_account_bindings(target, list(payload.user_ids), caller)
        return {
            "account_id": account_id,
            "bound_user_id": updated.bound_user_id,
            "bound_user_ids": updated.bound_user_ids,
        }

    @router.get("/workspaces", response_model=AccessibleKnowledgeResponse)
    async def list_accessible_workspaces(
        caller: WebUiPrincipal = principal_dep,
    ) -> AccessibleKnowledgeResponse:
        permissions = set(caller.permissions)
        visible_users: list[AccessibleUser] = []
        visible_workspaces: list[AccessibleWorkspace] = []
        for user in (await runtime.metadata.list_users()).users:
            can_read_user = await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.user.read",
                permission="webui.user.read",
            )
            if not can_read_user:
                continue
            can_create = await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.workspace.create",
                permission="webui.workspace.create",
            )
            can_manage_user_policy = await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.user.policy.manage",
                permission="webui.user.policy.manage",
            )
            can_rename_user = await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.user.rename",
                permission="webui.user.rename",
            )
            can_delete_user = await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.user.delete",
                permission="webui.user.delete",
            )
            summaries = list((await runtime.metadata.list_workspaces(user.user_id)).workspaces)
            if can_delete_user:
                for summary in summaries:
                    if not await policies.allows_workspace(
                        caller.account,
                        permissions,
                        workspace_id=summary.workspace_id,
                        action="webui.workspace.delete",
                        permission="webui.workspace.delete",
                        user_id=user.user_id,
                    ):
                        can_delete_user = False
                        break
            user_policy = await policies.get_user_policy(user.user_id)
            visible_users.append(
                AccessibleUser(
                    **user.model_dump(),
                    read_min_level=user_policy.read_min_level,
                    workspace_create_min_level=user_policy.workspace_create_min_level,
                    policy_version=user_policy.policy_version,
                    can_create_workspace=can_create,
                    can_manage_policy=can_manage_user_policy,
                    can_rename=can_rename_user,
                    can_delete=can_delete_user,
                )
            )
            for summary in summaries:
                workspace = await runtime.metadata.get_workspace(summary.workspace_id)
                if workspace is None:
                    continue
                item = await workspace_item(caller, workspace)
                if item.capabilities.can_read:
                    visible_workspaces.append(item)
        return AccessibleKnowledgeResponse(users=visible_users, workspaces=visible_workspaces)

    @router.delete("/users/{user_id}/access")
    async def leave_knowledge_user(
        user_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, str]:
        """退出知识域；个人拒绝规则会同步阻断其下知识库与 Agent 检索。"""

        known_users = {user.user_id for user in (await runtime.metadata.list_users()).users}
        allowed = user_id in known_users and await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=user_id,
            action="webui.user.read",
            permission="webui.user.read",
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        try:
            await policies.leave_resource_access(
                caller.account_id,
                resource_type="user",
                resource_id=user_id,
            )
        except InvalidPolicyBindingError as exc:
            raise _error(status.HTTP_409_CONFLICT, "resource_access_immutable", str(exc)) from exc
        if user_id in caller.account.bound_user_ids:
            remaining = [item for item in caller.account.bound_user_ids if item != user_id]
            await webui_store.set_account_user_bindings(
                caller.account_id,
                remaining,
                actor_account_id=caller.account_id,
            )
            await policies.replace_account_binding_access(
                caller.account_id,
                await account_binding_scope(remaining),
            )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.access.leave_self",
            resource_type="user",
            resource_id=user_id,
            before={"access": "allow"},
            after={"access": "deny"},
        )
        return {"status": "ok", "user_id": user_id}

    @router.delete("/workspaces/{workspace_id}/access")
    async def leave_workspace(
        workspace_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, str]:
        """退出单个知识库，并覆盖账号从权限组继承的该库权限。"""

        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.read",
            permission="webui.workspace.read",
        )
        try:
            await policies.leave_resource_access(
                caller.account_id,
                resource_type="workspace",
                resource_id=workspace_id,
            )
        except InvalidPolicyBindingError as exc:
            raise _error(status.HTTP_409_CONFLICT, "resource_access_immutable", str(exc)) from exc
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.access.leave_self",
            resource_type="workspace",
            resource_id=workspace_id,
            before={"access": "allow", "user_id": workspace.user_id},
            after={"access": "deny", "user_id": workspace.user_id},
        )
        return {"status": "ok", "workspace_id": workspace_id}

    @router.patch("/users/{user_id}")
    async def rename_knowledge_user(
        user_id: str,
        payload: RenameKnowledgeUserRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        """仅修改知识域展示名称，不改变 UserID 和其下知识库。"""

        allowed = await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=user_id,
            action="webui.user.rename",
            permission="webui.user.rename",
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        before = next(
            (item for item in (await runtime.metadata.list_users()).users if item.user_id == user_id),
            None,
        )
        if before is None:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        renamed = await runtime.metadata.rename_user(user_id, payload.user_name)
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.rename",
            resource_type="user",
            resource_id=user_id,
            before={"user_name": before.user_name},
            after={"user_name": renamed.user_name},
        )
        return renamed.model_dump(mode="json")

    @router.post("/workspaces", response_model=AccessibleWorkspace, status_code=status.HTTP_201_CREATED)
    async def create_workspace(
        payload: CreateWorkspaceRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> AccessibleWorkspace:
        known_users = {user.user_id for user in (await runtime.metadata.list_users()).users}
        if payload.user_id not in known_users:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        allowed = await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=payload.user_id,
            action="webui.workspace.create",
            permission="webui.workspace.create",
        )
        if not allowed:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "workspace creation is not allowed")
        _validate_thresholds(
            caller, payload.read_min_level, payload.cud_min_level, allowed_levels=allowed_levels
        )
        name = normalize_workspace_name(payload.workspace_name)
        workspace = WorkspaceRecord(
            workspace_id=f"workspace_{uuid4()}",
            user_id=payload.user_id,
            workspace_name=name,
        )
        try:
            await runtime.metadata.create_workspace(workspace)
            await policies.set_workspace_policy(
                workspace.workspace_id,
                read_min_level=payload.read_min_level,
                cud_min_level=payload.cud_min_level,
                actor_account_id=caller.account_id,
                creating=True,
            )
            await policies.ensure_resource_acl("workspace", workspace.workspace_id, user_id=workspace.user_id)
            await policies.grant_workspace_creator_access(caller.account_id, workspace.workspace_id)
            await policies.mark_lifecycle(workspace.workspace_id, "active", actor_account_id=caller.account_id)
            await runtime.elasticsearch.mirror_workspace(workspace)
        except sqlite3.IntegrityError as exc:
            raise _error(status.HTTP_409_CONFLICT, "workspace_conflict", "workspace name already exists") from exc
        except Exception:
            if await runtime.metadata.get_workspace(workspace.workspace_id):
                await runtime.metadata.delete_workspace(workspace.workspace_id)
            await policies.delete_workspace_policy_data(workspace.workspace_id)
            raise
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.create",
            resource_type="workspace",
            resource_id=workspace.workspace_id,
            after=workspace.model_dump(mode="json"),
        )
        return await workspace_item(caller, workspace)

    @router.patch("/workspaces/{workspace_id}", response_model=AccessibleWorkspace)
    async def rename_workspace(
        workspace_id: str,
        payload: RenameWorkspaceRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> AccessibleWorkspace:
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.rename",
            permission="webui.workspace.rename",
        )
        name = normalize_workspace_name(payload.workspace_name)
        async with runtime.tasks.workspace_lock(workspace.user_id, workspace_id):
            if await runtime.metadata.workspace_has_active_tasks(workspace_id):
                raise _error(status.HTTP_409_CONFLICT, "workspace_busy", "workspace has active tasks")
            try:
                renamed = await runtime.metadata.rename_workspace(workspace_id, name)
            except sqlite3.IntegrityError as exc:
                raise _error(
                    status.HTTP_409_CONFLICT, "workspace_name_conflict", "workspace name already exists"
                ) from exc
            try:
                await runtime.elasticsearch.rename_workspace(renamed)
            except Exception:
                restored = await runtime.metadata.rename_workspace(workspace_id, workspace.workspace_name)
                await runtime.elasticsearch.rename_workspace(restored)
                raise
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.rename",
            resource_type="workspace",
            resource_id=workspace_id,
            before={"workspace_name": workspace.workspace_name},
            after={"workspace_name": renamed.workspace_name},
        )
        return await workspace_item(caller, renamed)

    @router.put("/workspaces/{workspace_id}/policy", response_model=AccessibleWorkspace)
    async def update_workspace_policy(
        workspace_id: str,
        payload: WorkspacePolicyRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> AccessibleWorkspace:
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.policy.manage",
            permission="webui.workspace.policy.manage",
        )
        _validate_thresholds(
            caller, payload.read_min_level, payload.cud_min_level, allowed_levels=allowed_levels
        )
        before = await policies.get_workspace_policy(workspace_id)
        after = await policies.set_workspace_policy(
            workspace_id,
            read_min_level=payload.read_min_level,
            cud_min_level=payload.cud_min_level,
            actor_account_id=caller.account_id,
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.policy.update",
            resource_type="workspace",
            resource_id=workspace_id,
            before=asdict(before),
            after=asdict(after),
        )
        return await workspace_item(caller, workspace)

    @router.put("/users/{user_id}/policy")
    async def update_user_policy(
        user_id: str,
        payload: UserPolicyRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> dict[str, Any]:
        await require_user_policy_scope(caller, user_id)
        _validate_thresholds(
            caller,
            payload.read_min_level,
            payload.workspace_create_min_level,
            allowed_levels=allowed_levels,
        )
        before = await policies.get_user_policy(user_id)
        value = await policies.set_user_policy(
            user_id,
            read_min_level=payload.read_min_level,
            workspace_create_min_level=payload.workspace_create_min_level,
            actor_account_id=caller.account_id,
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.policy.update",
            resource_type="user",
            resource_id=user_id,
            before=asdict(before),
            after=asdict(value),
        )
        return asdict(value)

    @router.get("/users/{user_id}/bindings", response_model=PolicyBindingsResponse)
    async def get_user_bindings(
        user_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> PolicyBindingsResponse:
        await require_user_policy_scope(caller, user_id)
        values = await policies.list_bindings("user", user_id, USER_POLICY_ACTIONS)
        return binding_response("user", user_id, values)

    @router.put("/users/{user_id}/bindings", response_model=PolicyBindingsResponse)
    async def update_user_bindings(
        user_id: str,
        payload: PolicyBindingsUpdateRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> PolicyBindingsResponse:
        await require_user_policy_scope(caller, user_id)
        if payload.action not in USER_POLICY_ACTIONS:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_binding_action", "invalid user ACL action")
        if payload.action not in caller.permissions:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "permission_delegation_denied",
                "cannot grant an action the caller does not have",
            )
        caller_has_action = await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=user_id,
            action=payload.action,
            permission=payload.action,
        )
        if not caller_has_action:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "permission_delegation_denied",
                "cannot grant an action outside the caller's resource scope",
            )
        await validate_binding_targets(payload.action, payload.bindings)
        before = await policies.list_bindings("user", user_id, USER_POLICY_ACTIONS)
        try:
            values = await policies.replace_bindings(
                "user",
                user_id,
                payload.action,
                [PolicyBinding(**binding.model_dump()) for binding in payload.bindings],
                actor_account_id=caller.account_id,
            )
        except InvalidPolicyBindingError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_policy_binding", str(exc)) from exc
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.bindings.update",
            resource_type="user",
            resource_id=user_id,
            before={key: [asdict(item) for item in items] for key, items in before.items()},
            after={key: [asdict(item) for item in items] for key, items in values.items()},
        )
        return binding_response("user", user_id, values)

    @router.get("/workspaces/{workspace_id}/bindings", response_model=PolicyBindingsResponse)
    async def get_workspace_bindings(
        workspace_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> PolicyBindingsResponse:
        await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.policy.manage",
            permission="webui.workspace.policy.manage",
        )
        values = await policies.list_bindings("workspace", workspace_id, WORKSPACE_POLICY_ACTIONS)
        return binding_response("workspace", workspace_id, values)

    @router.put("/workspaces/{workspace_id}/bindings", response_model=PolicyBindingsResponse)
    async def update_workspace_bindings(
        workspace_id: str,
        payload: PolicyBindingsUpdateRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> PolicyBindingsResponse:
        await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.policy.manage",
            permission="webui.workspace.policy.manage",
        )
        if payload.action not in WORKSPACE_POLICY_ACTIONS:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_binding_action",
                "invalid workspace ACL action",
            )
        if payload.action not in caller.permissions:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "permission_delegation_denied",
                "cannot grant an action the caller does not have",
            )
        workspace = await runtime.metadata.get_workspace(workspace_id)
        if workspace is None:
            raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "workspace does not exist")
        caller_has_action = await policies.allows_workspace(
            caller.account,
            set(caller.permissions),
            workspace_id=workspace_id,
            action=payload.action,
            permission=payload.action,
            user_id=workspace.user_id,
        )
        if not caller_has_action:
            raise _error(
                status.HTTP_403_FORBIDDEN,
                "permission_delegation_denied",
                "cannot grant an action outside the caller's resource scope",
            )
        await validate_binding_targets(payload.action, payload.bindings)
        before = await policies.list_bindings("workspace", workspace_id, WORKSPACE_POLICY_ACTIONS)
        try:
            values = await policies.replace_bindings(
                "workspace",
                workspace_id,
                payload.action,
                [PolicyBinding(**binding.model_dump()) for binding in payload.bindings],
                actor_account_id=caller.account_id,
            )
        except InvalidPolicyBindingError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_policy_binding", str(exc)) from exc
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.bindings.update",
            resource_type="workspace",
            resource_id=workspace_id,
            before={key: [asdict(item) for item in items] for key, items in before.items()},
            after={key: [asdict(item) for item in items] for key, items in values.items()},
        )
        return binding_response("workspace", workspace_id, values)

    @router.delete("/workspaces/{workspace_id}", response_model=DeleteWorkspaceResponse)
    async def delete_workspace(
        workspace_id: str,
        caller: WebUiPrincipal = principal_dep,
        confirm_name: str = Query(min_length=1, max_length=128),
    ) -> DeleteWorkspaceResponse:
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.delete",
            permission="webui.workspace.delete",
        )
        if confirm_name != workspace.workspace_name:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "confirmation_mismatch", "workspace name does not match")
        operation_id = f"workspace-delete-{uuid4().hex}"
        lifecycle_detail = json.dumps(
            {"operation_id": operation_id, "user_id": workspace.user_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        recycled = None
        metadata_deleted = False
        cleanup_pending = False
        async with runtime.tasks.workspace_lock(workspace.user_id, workspace_id):
            locked_workspace = await runtime.metadata.get_workspace(workspace_id)
            lifecycle = await policies.lifecycle(workspace_id)
            if locked_workspace is None:
                raise _error(status.HTTP_404_NOT_FOUND, "workspace_not_found", "workspace was not found")
            if lifecycle not in {None, "active"}:
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "workspace_deleting",
                    "workspace deletion is already in progress",
                )
            if confirm_name != locked_workspace.workspace_name:
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "confirmation_mismatch",
                    "workspace name does not match",
                )
            workspace = locked_workspace
            if await runtime.metadata.workspace_has_active_tasks(workspace_id):
                raise _error(status.HTTP_409_CONFLICT, "workspace_busy", "workspace has active tasks")
            await policies.mark_lifecycle(
                workspace_id,
                "deleting",
                actor_account_id=caller.account_id,
                detail=lifecycle_detail,
            )
            try:
                recycled = await runtime.artifacts.move_workspace_to_recycle(
                    operation_id, workspace.user_id, workspace_id
                )
                await runtime.metadata.delete_workspace(workspace_id)
                metadata_deleted = True
                await policies.delete_workspace_policy_data(workspace_id)
                await runtime.elasticsearch.delete_workspace_contents(workspace_id)
                await runtime.milvus.delete_workspace(workspace_id)
                documents, chunks = await runtime.elasticsearch.count_workspace_contents(workspace_id)
                vectors = await runtime.milvus.count_workspace(workspace_id)
                if documents or chunks or vectors:
                    raise RuntimeError("workspace index cleanup verification failed")
                await runtime.artifacts.cleanup_recycle(operation_id)
                await policies.mark_lifecycle(workspace_id, "deleted", actor_account_id=caller.account_id)
            except Exception as exc:
                if not metadata_deleted:
                    current_workspace = await runtime.metadata.get_workspace(workspace_id)
                    if current_workspace is not None:
                        if recycled is not None:
                            await runtime.artifacts.restore_from_recycle(
                                recycled,
                                str(runtime.artifacts.workspace_dir(workspace.user_id, workspace_id)),
                            )
                        await policies.mark_lifecycle(
                            workspace_id,
                            "active",
                            actor_account_id=caller.account_id,
                            detail=type(exc).__name__,
                        )
                    else:
                        cleanup_pending = True
                        await policies.mark_lifecycle(
                            workspace_id,
                            "delete_failed",
                            actor_account_id=caller.account_id,
                            detail=json.dumps(
                                {
                                    "operation_id": operation_id,
                                    "user_id": workspace.user_id,
                                    "error_type": type(exc).__name__,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                else:
                    cleanup_pending = True
                    await policies.mark_lifecycle(
                        workspace_id,
                        "delete_failed",
                        actor_account_id=caller.account_id,
                        detail=json.dumps(
                            {
                                "operation_id": operation_id,
                                "user_id": workspace.user_id,
                                "error_type": type(exc).__name__,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                if not metadata_deleted and not cleanup_pending:
                    raise
        result_status: Literal["deleted", "cleanup_pending"] = "cleanup_pending" if cleanup_pending else "deleted"
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.workspace.delete",
            resource_type="workspace",
            resource_id=workspace_id,
            before=workspace.model_dump(mode="json"),
            after={"status": result_status, "operation_id": operation_id},
        )
        return DeleteWorkspaceResponse(status=result_status, workspace_id=workspace_id)

    @router.delete("/users/{user_id}", response_model=DeleteKnowledgeUserResponse)
    async def delete_knowledge_user(
        user_id: str,
        caller: WebUiPrincipal = principal_dep,
        confirm_name: str = Query(min_length=1, max_length=128),
    ) -> DeleteKnowledgeUserResponse:
        """级联删除知识域、其下全部 Workspace、默认绑定与策略。"""

        allowed = await policies.allows_user(
            caller.account,
            set(caller.permissions),
            user_id=user_id,
            action="webui.user.delete",
            permission="webui.user.delete",
        )
        if not allowed:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        user = next(
            (item for item in (await runtime.metadata.list_users()).users if item.user_id == user_id),
            None,
        )
        if user is None:
            raise _error(status.HTTP_404_NOT_FOUND, "user_not_found", "knowledge user does not exist")
        if confirm_name != user.user_name:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "confirmation_mismatch", "user name does not match")

        summaries = list((await runtime.metadata.list_workspaces(user_id)).workspaces)
        # 在开始级联前完成预检，避免因排队任务造成可预见的半删除。
        for summary in summaries:
            lifecycle = await policies.lifecycle(summary.workspace_id)
            if lifecycle not in {None, "active"}:
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "workspace_deleting",
                    f"workspace {summary.workspace_name} deletion is already in progress",
                )
            if await runtime.metadata.workspace_has_active_tasks(summary.workspace_id):
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "workspace_busy",
                    f"workspace {summary.workspace_name} has active tasks",
                )

        deleted_count = 0
        for summary in summaries:
            result = await delete_workspace(
                summary.workspace_id,
                caller,
                summary.workspace_name,
            )
            if result.status == "cleanup_pending":
                await webui_store.record_audit(
                    actor_account_id=caller.account_id,
                    action="webui.user.delete.cleanup_pending",
                    resource_type="user",
                    resource_id=user_id,
                    before=user.model_dump(mode="json"),
                    after={"deleted_workspace_count": deleted_count + 1},
                )
                return DeleteKnowledgeUserResponse(
                    status="cleanup_pending",
                    user_id=user_id,
                    deleted_workspace_count=deleted_count + 1,
                )
            deleted_count += 1

        cleanup_user_artifacts = getattr(runtime.artifacts, "delete_user_artifacts", None)
        if cleanup_user_artifacts is not None:
            await cleanup_user_artifacts(user_id)
        for account in await webui_store.list_accounts():
            if user_id in account.bound_user_ids:
                remaining = [item for item in account.bound_user_ids if item != user_id]
                await webui_store.set_account_user_bindings(
                    account.account_id,
                    remaining,
                    actor_account_id=caller.account_id,
                )
                await policies.replace_account_binding_access(
                    account.account_id,
                    await account_binding_scope(remaining),
                )
        await policies.delete_user_policy_data(user_id)
        await runtime.metadata.delete_user(user_id)
        await webui_store.remove_user_bindings(user_id)
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.user.delete",
            resource_type="user",
            resource_id=user_id,
            before=user.model_dump(mode="json"),
            after={"status": "deleted", "deleted_workspace_count": deleted_count},
        )
        return DeleteKnowledgeUserResponse(
            status="deleted",
            user_id=user_id,
            deleted_workspace_count=deleted_count,
        )

    @router.get("/workspaces/{workspace_id}/files", response_model=FileListResponse)
    async def list_files(
        workspace_id: str,
        caller: WebUiPrincipal = principal_dep,
        include_string_content: bool = False,
    ) -> FileListResponse:
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.read",
            permission="webui.workspace.read",
        )
        return await FileService(runtime).list_files(
            workspace.user_id,
            workspace_id,
            include_string_content=include_string_content,
        )

    @router.get("/workspaces/{workspace_id}/files/{file_id}", response_model=FileDetailResponse)
    async def get_file_detail(
        workspace_id: str,
        file_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> FileDetailResponse:
        await require_workspace(
            caller,
            workspace_id,
            action="webui.workspace.read",
            permission="webui.workspace.read",
        )
        resource = await runtime.metadata.get_file(workspace_id, file_id)
        if resource is None or resource.file_name is None:
            raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "file does not exist")
        downloadable = await policies.allows_workspace(
            caller.account,
            set(caller.permissions),
            workspace_id=workspace_id,
            action="webui.resource.file.download",
            permission="webui.resource.file.download",
            user_id=resource.user_id,
        )
        return FileDetailResponse(
            workspace_id=workspace_id,
            file_id=file_id,
            file_name=resource.file_name,
            mime_type=resource.mime_type,
            content_hash=resource.content_hash,
            markdown_hash=resource.markdown_hash,
            size_bytes=resource.size_bytes,
            parser=resource.parser,
            degraded=resource.degraded,
            chunk_count=resource.chunk_count,
            created_at=resource.created_at.isoformat(),
            modified_at=resource.modified_at.isoformat(),
            downloadable=downloadable,
        )

    @router.get("/workspaces/{workspace_id}/files/{file_id}/download")
    async def download_file(
        workspace_id: str,
        file_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> FileResponse:
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.resource.file.download",
            permission="webui.resource.file.download",
        )
        resource = await runtime.metadata.get_file(workspace_id, file_id)
        if resource is None or resource.file_name is None:
            raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "file does not exist")
        try:
            target = runtime.artifacts.raw_file_path(resource)
        except FileNotFoundError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "raw file is unavailable") from exc
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.resource.file.download",
            resource_type="file",
            resource_id=file_id,
            before={"workspace_id": workspace.workspace_id, "file_name": resource.file_name},
        )
        return FileResponse(
            target,
            media_type=resource.mime_type or "application/octet-stream",
            filename=resource.file_name,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/workspaces/{workspace_id}/resources", response_model=TaskAccepted, status_code=202)
    async def add_resource(
        workspace_id: str,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> TaskAccepted:
        _verify_same_origin(request)
        form = await request.form()
        source_type = str(form.get("type") or "")
        if source_type == "file":
            permission = "webui.resource.file.add"
        elif source_type == "str":
            permission = "webui.resource.text.add"
        else:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "type must be file or str")
        workspace = await require_workspace(
            caller,
            workspace_id,
            action=permission,
            permission=permission,
        )
        if source_type == "file":
            uploaded = form.get("file")
            if not isinstance(uploaded, UploadFile):
                raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "file is required")
            content = await uploaded.read(runtime.settings.max_file_bytes + 1)
            source = FileSource(
                file_name=uploaded.filename or "upload.bin",
                mime_type=uploaded.content_type or "application/octet-stream",
                content_base64=base64.b64encode(content).decode("ascii"),
            )
            audit_source = {
                "source_type": source_type,
                "file_name": source.file_name,
                "mime_type": source.mime_type,
                "size_bytes": len(content),
            }
        else:
            source = StringSource(content=str(form.get("content") or ""))
            audit_source = {
                "source_type": source_type,
                "size_bytes": len(source.content.encode("utf-8")),
            }
        accepted = await FileService(runtime).submit_add(
            AddRequest(
                user_id=workspace.user_id,
                workspace_id=workspace_id,
                workspace_name=workspace.workspace_name,
                source=source,
            )
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.resource.add.submit",
            resource_type="workspace",
            resource_id=workspace_id,
            after={**audit_source, **accepted.model_dump(mode="json")},
        )
        return accepted

    @router.delete("/workspaces/{workspace_id}/files/{file_id}", response_model=TaskAccepted, status_code=202)
    async def delete_file(
        workspace_id: str,
        file_id: str,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> TaskAccepted:
        _verify_same_origin(request)
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.resource.file.delete",
            permission="webui.resource.file.delete",
        )
        resource = await runtime.metadata.get_file(workspace_id, file_id)
        if resource is None or resource.file_name is None:
            raise _error(status.HTTP_404_NOT_FOUND, "file_not_found", "file does not exist")
        accepted = await FileService(runtime).submit_delete(
            DeleteFileRequest(
                user_id=workspace.user_id,
                workspace_id=workspace_id,
                file_id=file_id,
                file_name=resource.file_name,
            )
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.resource.file_delete.submit",
            resource_type="file",
            resource_id=file_id,
            before={"workspace_id": workspace_id, "file_name": resource.file_name},
            after=accepted.model_dump(mode="json"),
        )
        return accepted

    @router.delete("/workspaces/{workspace_id}/strings/{content_hash}", response_model=TaskAccepted, status_code=202)
    async def delete_string(
        workspace_id: str,
        content_hash: str,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> TaskAccepted:
        _verify_same_origin(request)
        workspace = await require_workspace(
            caller,
            workspace_id,
            action="webui.resource.text.delete",
            permission="webui.resource.text.delete",
        )
        accepted = await FileService(runtime).submit_delete_string(
            DeleteStringRequest(
                user_id=workspace.user_id,
                workspace_id=workspace_id,
                content_hash=content_hash,
            )
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.resource.string_delete.submit",
            resource_type="string",
            resource_id=content_hash,
            before={"workspace_id": workspace_id},
            after=accepted.model_dump(mode="json"),
        )
        return accepted

    @router.post("/retrieval", response_model=RetrievalResponse)
    async def webui_retrieval(
        payload: WebUiRetrievalRequest,
        caller: WebUiPrincipal = principal_dep,
    ) -> RetrievalResponse:
        if "webui.retrieval.use" not in caller.permissions:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "retrieval is not allowed")
        workspace = await require_workspace(
            caller,
            payload.workspace_id,
            action="webui.workspace.read",
            permission="webui.workspace.read",
        )
        result = await retrieve(
            runtime,
            RetrievalRequest(
                user_id=workspace.user_id,
                workspace_id=workspace.workspace_id,
                query=payload.query,
                top_k=payload.top_k,
            ),
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.retrieval.use",
            resource_type="workspace",
            resource_id=workspace.workspace_id,
            after={"top_k": payload.top_k},
        )
        return result

    @router.post("/chat/stream", response_class=StreamingResponse)
    async def webui_chat(
        payload: WebUiChatRequest,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> StreamingResponse:
        if "webui.chat.use" not in caller.permissions:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "chat is not allowed")
        permissions = set(caller.permissions)
        allowed_workspaces: list[WorkspaceRecord] = []
        for user in (await runtime.metadata.list_users()).users:
            if not await policies.allows_user(
                caller.account,
                permissions,
                user_id=user.user_id,
                action="webui.user.read",
                permission="webui.user.read",
            ):
                continue
            for summary in (await runtime.metadata.list_workspaces(user.user_id)).workspaces:
                if not await policies.allows_workspace(
                    caller.account,
                    permissions,
                    workspace_id=summary.workspace_id,
                    action="webui.workspace.read",
                    permission="webui.workspace.read",
                    user_id=user.user_id,
                ):
                    continue
                workspace = await runtime.metadata.get_workspace(summary.workspace_id)
                if workspace is not None:
                    allowed_workspaces.append(workspace)
        conversation_id = payload.conversation_id or uuid4().hex
        chat_request = ChatRequest(
            user_id=caller.bound_user_id or caller.account.login_name,
            messages=payload.messages,
            top_k=payload.top_k,
            conversation_id=conversation_id,
        )
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.chat.use",
            resource_type="agent",
            resource_id=caller.account_id,
            after={
                "allowed_workspace_ids": [workspace.workspace_id for workspace in allowed_workspaces],
                "conversation_id": payload.conversation_id,
            },
        )
        context = None
        if agent_registry is not None:
            session_token = request.cookies.get(cookie_name)

            async def resolve_principal() -> WebUiPrincipal:
                if not session_token:
                    raise _error(status.HTTP_401_UNAUTHORIZED, "session_required", "session is required")
                account = await webui_store.authenticate_session(session_token)
                if account is None:
                    raise _error(status.HTTP_401_UNAUTHORIZED, "invalid_session", "session is invalid")
                permissions = await webui_store.permission_keys(account.account_id)
                return WebUiPrincipal(account=account, permissions=frozenset(permissions))

            context = AgentContext(
                principal=caller,
                runtime=runtime,
                store=webui_store,
                policies=policies,
                resolve_principal=resolve_principal,
                conversation_id=conversation_id,
                action_store=agent_store,
                capability_gateway=agent_capabilities,
            )
        return StreamingResponse(
            stream_chat(
                runtime,
                chat_request,
                allowed_workspaces=allowed_workspaces,
                agent_context=context,
                agent_registry=agent_registry,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(
        task_id: str,
        caller: WebUiPrincipal = principal_dep,
    ) -> TaskResponse:
        record = await runtime.metadata.get_task(task_id)
        if record is None:
            raise _error(status.HTTP_404_NOT_FOUND, "task_not_found", "task does not exist")
        await require_workspace(
            caller,
            record.workspace_id,
            action="webui.workspace.read",
            permission="webui.workspace.read",
        )
        return await FileService(runtime).get_task(task_id, record.user_id)

    @router.delete("/tasks/{task_id}", response_model=TaskResponse)
    async def cancel_task(
        task_id: str,
        request: Request,
        caller: WebUiPrincipal = principal_dep,
    ) -> TaskResponse:
        """取消当前账号有权写入的排队中或执行中的入库任务。"""

        _verify_same_origin(request)
        record = await runtime.metadata.get_task(task_id)
        if record is None:
            raise _error(status.HTTP_404_NOT_FOUND, "task_not_found", "task does not exist")
        permission = (
            "webui.resource.file.add"
            if record.operation == "add_file"
            else "webui.resource.text.add"
            if record.operation == "add_str"
            else ""
        )
        if not permission:
            raise _error(
                status.HTTP_409_CONFLICT,
                "task_not_cancellable",
                "only ingestion tasks can be cancelled",
            )
        await require_workspace(
            caller,
            record.workspace_id,
            action=permission,
            permission=permission,
        )
        response = await FileService(runtime).cancel_ingestion(task_id, record.user_id)
        await webui_store.record_audit(
            actor_account_id=caller.account_id,
            action="webui.resource.ingestion.cancel",
            resource_type="task",
            resource_id=task_id,
            before={"status": record.status.value, "stage": record.stage},
            after={"status": response.status.value, "stage": response.stage},
        )
        return response

    @router.get("/system/health", response_model=HealthResponse)
    async def system_health(
        caller: WebUiPrincipal = principal_dep,
    ) -> HealthResponse:
        if "webui.system.read" not in caller.permissions:
            raise _error(status.HTTP_403_FORBIDDEN, "permission_denied", "system health is not allowed")
        return await dependency_health(runtime)

    return router


def _validate_thresholds(
    caller: WebUiPrincipal,
    *levels: int,
    allowed_levels: frozenset[int],
) -> None:
    if any(level not in allowed_levels for level in levels):
        allowed = ", ".join(str(level) for level in sorted(allowed_levels))
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_permission_level",
            f"permission level must be one of: {allowed}",
        )
    if any(level > caller.account.permission_level for level in levels):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "level_escalation_denied",
            "resource thresholds cannot exceed the caller's level",
        )


def _verify_same_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        raise _error(status.HTTP_403_FORBIDDEN, "origin_denied", "cross-site mutation is not allowed")
    origin = request.headers.get("origin")
    forwarded_host = request.headers.get("x-forwarded-host")
    if not origin or not forwarded_host:
        return
    expected = f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{forwarded_host}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise _error(status.HTTP_403_FORBIDDEN, "origin_denied", "cross-origin mutation is not allowed")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
