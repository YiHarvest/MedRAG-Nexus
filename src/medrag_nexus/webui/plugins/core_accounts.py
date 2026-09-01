"""账号、审计和权限组管理节点。"""

from __future__ import annotations

from ..permissions import PermissionRegistry, PluginManifest


class CoreAccountsPlugin:
    manifest = PluginManifest(plugin_id="webui.core_accounts", version="2.0.0")

    def register(self, registry: PermissionRegistry) -> None:
        for value, name, description in (
            (0, "初级用户", "默认注册账号"),
            (1, "VIP 用户", "知识资源编辑者"),
            (2, "知识库管理员", "可管理知识库的成员"),
            (1000, "超级管理员", "拥有全部系统管理权限"),
        ):
            registry.register_level(value, name, description)
        descriptions = {
            "webui.account.create": "创建登录用户",
            "webui.account.create_superadmin": "创建同级超级管理员",
            "webui.account.manage": "查看和修改普通登录用户",
            "webui.account.update_self": "修改自己的用户资料",
            "webui.account.password.change_self": "修改自己的密码",
            "webui.account.password.reset": "重置普通用户密码",
            "webui.audit.read": "读取 WebUI 安全审计",
            "webui.permission.catalog.read": "读取权限插件目录",
            "webui.permission.group.manage": "管理自定义权限组",
        }
        protected = {
            "webui.account.create",
            "webui.account.create_superadmin",
            "webui.permission.group.manage",
        }
        for key, description in descriptions.items():
            registry.register_node(key, description, custom_assignable=key not in protected)
