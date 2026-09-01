"""感知实时权限的 WebUI Agent 函数工具注册表。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .context import AgentAuthorizationError, AgentContext

RiskLevel = Literal["read", "write", "sensitive", "destructive"]
InputMode = Literal["model", "file_picker", "secure_form", "confirmation"]
ToolHandler = Callable[[AgentContext, Mapping[str, Any]], Awaitable[Any]]


class AgentToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    schema: Mapping[str, Any]
    handler: ToolHandler
    required_permissions: tuple[str, ...] = ()
    superadmin_guard: bool = False
    risk_level: RiskLevel = "read"
    input_mode: InputMode = "model"

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.schema),
            },
        }


class AgentToolRegistry:
    def __init__(self, tools: Iterable[ToolSpec] = ()):
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            self.register(tool)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate Agent tool: {tool.name}")
        if not tool.name or not tool.name.replace("_", "").isalnum():
            raise ValueError(f"invalid Agent tool name: {tool.name!r}")
        if tool.schema.get("type") != "object":
            raise ValueError(f"Agent tool schema must be an object: {tool.name}")
        self._tools[tool.name] = tool

    def available_specs(self, context: AgentContext) -> tuple[ToolSpec, ...]:
        """使用最近一次解析的身份过滤可见工具。"""

        principal = context.principal
        return tuple(tool for tool in self._tools.values() if self._allows(tool, principal))

    def available_tools(self, context: AgentContext) -> list[dict[str, Any]]:
        return [tool.as_openai_tool() for tool in self.available_specs(context)]

    async def refresh_available_tools(self, context: AgentContext) -> list[dict[str, Any]]:
        await context.refresh_principal()
        return self.available_tools(context)

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: AgentContext,
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentToolError("unknown_tool", f"unknown Agent tool: {name}")
        principal = await context.refresh_principal()
        if not self._allows(tool, principal):
            missing = sorted(set(tool.required_permissions) - set(principal.permissions))
            detail = f"missing permission: {', '.join(missing)}" if missing else "superadmin permission is required"
            raise AgentAuthorizationError("permission_denied", detail)
        if not isinstance(arguments, Mapping):
            raise AgentToolError("invalid_arguments", "tool arguments must be a JSON object")
        used_permissions = context.metadata.setdefault("used_permissions", set())
        if isinstance(used_permissions, set):
            used_permissions.update(tool.required_permissions)
        result = await tool.handler(context, arguments)
        self._record_resource_requirements(context, tool, arguments, result)
        return result

    @staticmethod
    def _record_resource_requirements(
        context: AgentContext,
        tool: ToolSpec,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> None:
        """记录本轮实际读取的资源，供后续导出制品沿用权限交集。"""

        resources = context.metadata.setdefault("used_resources", {})
        if not isinstance(resources, dict):
            return

        def remember(workspace_id: str, permission: str = "webui.workspace.read") -> None:
            resources[("workspace", workspace_id, permission)] = {
                "resource_type": "workspace",
                "resource_id": workspace_id,
                "required_permission": permission,
            }

        def remember_user(user_id: str, permission: str = "webui.user.read") -> None:
            resources[("user", user_id, permission)] = {
                "resource_type": "user",
                "resource_id": user_id,
                "required_permission": permission,
            }

        workspace_id = arguments.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            permission = next(
                (
                    key
                    for key in tool.required_permissions
                    if key.startswith("webui.workspace.") or key.startswith("webui.resource.")
                ),
                "webui.workspace.read",
            )
            remember(workspace_id, permission)
        if isinstance(result, Mapping) and isinstance(result.get("citations"), list):
            for citation in result["citations"]:
                if isinstance(citation, Mapping) and isinstance(citation.get("workspace_id"), str):
                    remember(citation["workspace_id"])
        if isinstance(result, Mapping) and isinstance(result.get("workspaces"), list):
            for workspace in result["workspaces"]:
                if isinstance(workspace, Mapping) and isinstance(workspace.get("workspace_id"), str):
                    remember(workspace["workspace_id"])
        if isinstance(result, Mapping) and isinstance(result.get("users"), list):
            for user in result["users"]:
                if isinstance(user, Mapping) and isinstance(user.get("user_id"), str):
                    remember_user(user["user_id"])

    @staticmethod
    def _allows(tool: ToolSpec, principal: Any) -> bool:
        if principal.account.must_change_password or not principal.account.enabled:
            return False
        if tool.superadmin_guard and principal.account.permission_level < 1000:
            return False
        granted = set(principal.permissions)
        return all(permission in granted for permission in tool.required_permissions)


def object_schema(
    properties: Mapping[str, Any] | None = None,
    *,
    required: Iterable[str] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": additional_properties,
    }
