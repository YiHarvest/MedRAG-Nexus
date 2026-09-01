"""Agent 管理工具。

所有安全敏感修改都先表示为待确认意图；注册表绝不直接修改账号、权限组、密码或 ACL。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..context import AgentContext
from ..registry import ToolSpec, object_schema
from .read import _required_string, _string


def _intent_handler(
    tool_name: str,
    permissions: tuple[str, ...],
    *,
    target_type: str | None = None,
    target_key: str | None = None,
    confirmation_mode: str = "click",
    risk_level: str = "sensitive",
):
    async def handler(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
        target = None
        if target_type is not None and target_key is not None:
            target = {
                "resource_type": target_type,
                "resource_id": _required_string(arguments, target_key),
            }
        return await context.request_action(
            tool_name=tool_name,
            arguments=arguments,
            required_permissions=permissions,
            risk_level=risk_level,
            target=target,
            confirmation_mode=confirmation_mode,
        )

    return handler


async def update_own_profile(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    display_name = _required_string(arguments, "display_name")
    return await context.invoke_capability("update_own_profile", {"display_name": display_name})


async def change_own_password(context: AgentContext, _arguments: Mapping[str, Any]) -> Any:
    # 工具定义和参数都明确排除密码值。
    return await context.invoke_capability("change_own_password_secure_form", {})


async def revoke_artifact(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    artifact_id = _required_string(arguments, "artifact_id")
    return await context.request_action(
        tool_name="revoke_artifact",
        arguments={"artifact_id": artifact_id},
        required_permissions=("webui.agent.export",),
        risk_level="write",
        target={"resource_type": "agent_artifact", "resource_id": artifact_id},
        confirmation_mode="click",
    )


def administration_tool_specs() -> tuple[ToolSpec, ...]:
    account_id = _string("账号 ID")
    group_key = _string("权限组 key")
    user_id = _string("知识域 ID")
    workspace_id = _string("知识库 ID")
    permissions_array = {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 500}
    bindings_array = {
        "type": "array",
        "maxItems": 500,
        "items": {
            "type": "object",
            "properties": {
                "principal_type": {"type": "string", "enum": ["account", "group"]},
                "principal_id": _string("主体 ID"),
                "effect": {"type": "string", "enum": ["allow", "deny"]},
            },
            "required": ["principal_type", "principal_id", "effect"],
            "additionalProperties": False,
        },
    }
    specs = [
        ToolSpec(
            "update_own_profile",
            "修改当前账号显示名称。",
            object_schema({"display_name": _string("显示名称")}, required=("display_name",)),
            update_own_profile,
            ("webui.account.update_self",),
            risk_level="write",
        ),
        ToolSpec(
            "change_own_password",
            "打开安全密码表单修改当前账号密码；密码不经过模型。",
            object_schema(),
            change_own_password,
            ("webui.account.password.change_self",),
            risk_level="sensitive",
            input_mode="secure_form",
        ),
        ToolSpec(
            "revoke_artifact",
            "提前撤销当前账号有权管理的临时导出制品；只创建点击确认单。",
            object_schema({"artifact_id": _string("制品 ID")}, required=("artifact_id",)),
            revoke_artifact,
            ("webui.agent.export",),
            risk_level="write",
            input_mode="confirmation",
        ),
        ToolSpec(
            "create_account",
            "创建登录账号；仅创建确认单，密码在确认后的安全表单填写。",
            object_schema(
                {
                    "login_name": _string("登录名", max_length=64),
                    "display_name": _string("显示名称"),
                    "permission_level": {"type": "integer", "minimum": 0},
                    "group_keys": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    "must_change_password": {"type": "boolean", "default": True},
                },
                required=("login_name", "display_name"),
            ),
            _intent_handler("create_account", ("webui.account.create",)),
            ("webui.account.create",),
            superadmin_guard=True,
            risk_level="sensitive",
            input_mode="secure_form",
        ),
        ToolSpec(
            "update_account",
            "修改普通账号状态、等级或权限组；仅创建二次确认单。",
            object_schema(
                {
                    "account_id": account_id,
                    "display_name": _string("显示名称"),
                    "permission_level": {"type": "integer", "minimum": 0},
                    "enabled": {"type": "boolean"},
                    "group_keys": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    "must_change_password": {"type": "boolean"},
                },
                required=("account_id",),
            ),
            _intent_handler(
                "update_account", ("webui.account.manage",), target_type="account", target_key="account_id"
            ),
            ("webui.account.manage",),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "reset_account_password",
            "重置普通账号密码；确认后由安全表单收集密码。",
            object_schema(
                {"account_id": account_id, "must_change_password": {"type": "boolean", "default": True}},
                required=("account_id",),
            ),
            _intent_handler(
                "reset_account_password",
                ("webui.account.password.reset",),
                target_type="account",
                target_key="account_id",
            ),
            ("webui.account.password.reset",),
            risk_level="sensitive",
            input_mode="secure_form",
        ),
        ToolSpec(
            "bind_account_to_user",
            "为普通账号增加或移除一个知识域绑定；每个账号可绑定多个知识域，仅创建二次确认单。",
            object_schema(
                {"account_id": account_id, "user_id": user_id, "bound": {"type": "boolean", "default": True}},
                required=("account_id", "user_id"),
            ),
            _intent_handler(
                "bind_account_to_user", ("webui.user.binding.manage",), target_type="account", target_key="account_id"
            ),
            ("webui.user.binding.manage",),
            superadmin_guard=True,
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "create_permission_group",
            "创建权限组；仅创建二次确认单。",
            object_schema(
                {
                    "group_key": group_key,
                    "name": _string("权限组名称"),
                    "description": {"type": "string", "maxLength": 256},
                    "permissions": permissions_array,
                },
                required=("group_key", "permissions"),
            ),
            _intent_handler("create_permission_group", ("webui.permission.group.manage",)),
            ("webui.permission.group.manage",),
            superadmin_guard=True,
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "update_permission_group",
            "修改权限组；仅创建二次确认单。",
            object_schema(
                {
                    "group_key": group_key,
                    "name": _string("权限组名称"),
                    "description": {"type": "string", "maxLength": 256},
                    "permissions": permissions_array,
                },
                required=("group_key",),
            ),
            _intent_handler(
                "update_permission_group",
                ("webui.permission.group.manage",),
                target_type="permission_group",
                target_key="group_key",
            ),
            ("webui.permission.group.manage",),
            superadmin_guard=True,
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "delete_permission_group",
            "删除权限组；仅创建二次确认单。",
            object_schema({"group_key": group_key}, required=("group_key",)),
            _intent_handler(
                "delete_permission_group",
                ("webui.permission.group.manage",),
                target_type="permission_group",
                target_key="group_key",
                risk_level="destructive",
            ),
            ("webui.permission.group.manage",),
            superadmin_guard=True,
            risk_level="destructive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "leave_own_permission_group",
            "退出当前账号所属权限组；仅创建二次确认单。",
            object_schema({"group_key": group_key}, required=("group_key",)),
            _intent_handler("leave_own_permission_group", (), target_type="permission_group", target_key="group_key"),
            (),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "update_user_policy",
            "修改知识域等级阈值；仅创建二次确认单。",
            object_schema(
                {
                    "user_id": user_id,
                    "read_min_level": {"type": "integer", "minimum": 0},
                    "workspace_create_min_level": {"type": "integer", "minimum": 0},
                },
                required=("user_id", "read_min_level", "workspace_create_min_level"),
            ),
            _intent_handler(
                "update_user_policy", ("webui.user.policy.manage",), target_type="user", target_key="user_id"
            ),
            ("webui.user.policy.manage",),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "update_workspace_policy",
            "修改知识库等级阈值；仅创建二次确认单。",
            object_schema(
                {
                    "workspace_id": workspace_id,
                    "read_min_level": {"type": "integer", "minimum": 0},
                    "cud_min_level": {"type": "integer", "minimum": 0},
                },
                required=("workspace_id", "read_min_level", "cud_min_level"),
            ),
            _intent_handler(
                "update_workspace_policy",
                ("webui.workspace.policy.manage",),
                target_type="workspace",
                target_key="workspace_id",
            ),
            ("webui.workspace.policy.manage",),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "replace_user_bindings",
            "替换知识域某项 ACL；仅创建二次确认单。",
            object_schema(
                {"user_id": user_id, "action": _string("权限节点"), "bindings": bindings_array},
                required=("user_id", "action", "bindings"),
            ),
            _intent_handler(
                "replace_user_bindings", ("webui.user.policy.manage",), target_type="user", target_key="user_id"
            ),
            ("webui.user.policy.manage",),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
        ToolSpec(
            "replace_workspace_bindings",
            "替换知识库某项 ACL；仅创建二次确认单。",
            object_schema(
                {"workspace_id": workspace_id, "action": _string("权限节点"), "bindings": bindings_array},
                required=("workspace_id", "action", "bindings"),
            ),
            _intent_handler(
                "replace_workspace_bindings",
                ("webui.workspace.policy.manage",),
                target_type="workspace",
                target_key="workspace_id",
            ),
            ("webui.workspace.policy.manage",),
            risk_level="sensitive",
            input_mode="confirmation",
        ),
    ]
    return tuple(specs)
