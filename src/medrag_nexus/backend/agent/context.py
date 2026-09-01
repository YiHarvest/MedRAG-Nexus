"""后端 Agent 工具的运行上下文与鉴权辅助能力。

上下文只携带服务端解析出的 :class:`AccountPrincipal`；工具参数可以标识资源，但绝不能选择执行账号。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from medrag_nexus.backend.account_router import AccountPrincipal

PrincipalResolver = Callable[[], Awaitable[AccountPrincipal]]


class ActionIntentStore(Protocol):
    """由 Agent 持久化操作存储实现的最小适配边界。"""

    async def create_action(
        self,
        *,
        account_id: str,
        conversation_id: str,
        tool_name: str,
        canonical_arguments: Mapping[str, Any],
        required_permissions: tuple[str, ...] = (),
        target: Any | None = None,
        risk_level: str = "sensitive",
        confirmation_mode: str = "click",
        **kwargs: Any,
    ) -> Any: ...


class AgentCapabilityGateway(Protocol):
    """用于界面输入以及生成、下载制品的可选能力边界。"""

    async def invoke(
        self,
        capability: str,
        *,
        context: AgentContext,
        arguments: Mapping[str, Any],
    ) -> Any: ...


class AgentAuthorizationError(PermissionError):
    """当前实时权限拒绝工具操作时抛出。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class AgentContext:
    """执行 Agent 工具时使用的依赖和当前身份。

    ``principal`` 用于初次发现工具；执行时必须通过 ``resolve_principal`` 重新解析当前会话和账号，
    从而让权限组变化和账号停用立即生效。
    """

    principal: AccountPrincipal
    runtime: Any
    store: Any
    policies: Any
    resolve_principal: PrincipalResolver
    conversation_id: str
    action_store: ActionIntentStore | None = None
    capability_gateway: AgentCapabilityGateway | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    async def refresh_principal(self) -> AccountPrincipal:
        principal = await self.resolve_principal()
        if principal.account.account_id != self.principal.account.account_id:
            raise AgentAuthorizationError(
                "principal_changed",
                "the authenticated account changed during the Agent operation",
            )
        if not principal.account.enabled:
            raise AgentAuthorizationError("account_disabled", "the account is disabled")
        if principal.account.must_change_password:
            raise AgentAuthorizationError(
                "password_change_required",
                "password must be changed before using Agent tools",
            )
        self.principal = principal
        return principal

    async def allows_user(self, user_id: str, permission: str) -> bool:
        principal = self.principal
        return await self.policies.allows_user(
            principal.account,
            set(principal.permissions),
            user_id=user_id,
            action=permission,
            permission=permission,
        )

    async def allows_workspace(self, workspace_id: str, permission: str) -> bool:
        workspace = await self.runtime.metadata.get_workspace(workspace_id)
        if workspace is None:
            return False
        if not await self.allows_user(workspace.user_id, "webui.user.read"):
            return False
        principal = self.principal
        return await self.policies.allows_workspace(
            principal.account,
            set(principal.permissions),
            workspace_id=workspace_id,
            action=permission,
            permission=permission,
            user_id=workspace.user_id,
        )

    async def require_user(self, user_id: str, permission: str) -> None:
        if not await self.allows_user(user_id, permission):
            # 与 WebUI 一致：不向越权调用者披露目标资源是否存在。
            raise AgentAuthorizationError("user_not_found", "knowledge user does not exist")

    async def require_workspace(self, workspace_id: str, permission: str) -> Any:
        workspace = await self.runtime.metadata.get_workspace(workspace_id)
        if workspace is None or not await self.allows_workspace(workspace_id, permission):
            raise AgentAuthorizationError("workspace_not_found", "workspace does not exist")
        return workspace

    async def request_action(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        required_permissions: tuple[str, ...],
        risk_level: str,
        target: Any | None = None,
        confirmation_mode: str = "click",
    ) -> dict[str, Any]:
        """持久化确认意图，但不执行危险操作。"""

        if self.action_store is None:
            return {
                "status": "confirmation_required",
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "required_permissions": list(required_permissions),
                "risk_level": risk_level,
                "confirmation_mode": confirmation_mode,
            }
        created = await self.action_store.create_action(
            account_id=self.principal.account_id,
            conversation_id=self.conversation_id,
            tool_name=tool_name,
            canonical_arguments=dict(arguments),
            required_permissions=required_permissions,
            target=target,
            risk_level=risk_level,
            confirmation_mode=confirmation_mode,
        )
        payload = _jsonable(created)
        return {
            "status": "confirmation_required",
            "action": payload,
            "confirmation_mode": confirmation_mode,
        }

    async def invoke_capability(self, capability: str, arguments: Mapping[str, Any]) -> Any:
        if self.capability_gateway is None:
            return {
                "status": "input_required",
                "capability": capability,
                "arguments": dict(arguments),
            }
        return await self.capability_gateway.invoke(capability, context=self, arguments=arguments)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    return value
