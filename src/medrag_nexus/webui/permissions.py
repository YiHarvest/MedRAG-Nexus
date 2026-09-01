"""WebUI 权限插件注册器与权限计算基础设施。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """权限插件的稳定身份与依赖声明。"""

    plugin_id: str
    version: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PermissionLevel:
    value: int
    name: str
    description: str
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PermissionNode:
    key: str
    description: str
    plugin_id: str
    available: bool = True
    custom_assignable: bool = True


@dataclass(frozen=True, slots=True)
class PermissionGroup:
    key: str
    description: str
    permissions: frozenset[str]
    system_managed: bool = True


class PermissionPlugin(Protocol):
    manifest: PluginManifest

    def register(self, registry: PermissionRegistry) -> None: ...


class PermissionRegistry:
    """按依赖顺序收集插件定义，完成后冻结为只读目录。"""

    def __init__(self) -> None:
        self._nodes: dict[str, PermissionNode] = {}
        self._groups: dict[str, PermissionGroup] = {}
        self._levels: dict[int, PermissionLevel] = {}
        self._plugins: dict[str, PluginManifest] = {}
        self._frozen = False
        self._registering_plugin: str | None = None

    @property
    def nodes(self) -> tuple[PermissionNode, ...]:
        return tuple(self._nodes.values())

    @property
    def groups(self) -> tuple[PermissionGroup, ...]:
        return tuple(self._groups.values())

    @property
    def levels(self) -> tuple[PermissionLevel, ...]:
        return tuple(self._levels[value] for value in sorted(self._levels))

    @property
    def plugins(self) -> tuple[PluginManifest, ...]:
        return tuple(self._plugins.values())

    def register_level(self, value: int, name: str, description: str) -> None:
        self._assert_mutable()
        if value < 0:
            raise ValueError("permission level cannot be negative")
        current = PermissionLevel(value, name, description)
        previous = self._levels.get(value)
        if previous is not None and previous != current:
            raise ValueError(f"duplicate permission level: {value}")
        self._levels[value] = current

    def set_level_permissions(self, value: int, permissions: set[str] | frozenset[str]) -> None:
        """为成员等级绑定固有权限；它与可叠加的组织权限组相互独立。"""

        self._assert_mutable()
        current = self._levels.get(value)
        if current is None:
            raise ValueError(f"permission level is not registered: {value}")
        unknown = sorted(set(permissions) - self._nodes.keys())
        if unknown:
            raise ValueError(f"level {value} references unknown permissions: {', '.join(unknown)}")
        self._levels[value] = PermissionLevel(
            current.value,
            current.name,
            current.description,
            frozenset(permissions),
        )

    def register_node(
        self,
        key: str,
        description: str,
        *,
        plugin_id: str | None = None,
        custom_assignable: bool = True,
    ) -> None:
        self._assert_mutable()
        self._validate_key(key)
        owner = plugin_id or self._registering_plugin
        if owner is None:
            raise ValueError("permission nodes must be registered by a plugin")
        if owner not in self._plugins:
            raise ValueError(f"permission plugin is not registered: {owner}")
        if self._registering_plugin is not None and owner != self._registering_plugin:
            raise ValueError(f"plugin {self._registering_plugin} cannot register nodes for {owner}")
        if key in self._nodes:
            raise ValueError(f"duplicate permission node: {key}")
        self._nodes[key] = PermissionNode(
            key=key,
            description=description,
            plugin_id=owner,
            available=True,
            custom_assignable=custom_assignable,
        )

    def register_group(
        self,
        key: str,
        description: str,
        permissions: set[str] | frozenset[str],
        *,
        system_managed: bool = True,
    ) -> None:
        self._assert_mutable()
        self._validate_key(key)
        unknown = sorted(set(permissions) - self._nodes.keys())
        if unknown:
            raise ValueError(f"group {key} references unknown permissions: {', '.join(unknown)}")
        if key in self._groups:
            raise ValueError(f"duplicate permission group: {key}")
        self._groups[key] = PermissionGroup(key, description, frozenset(permissions), system_managed)

    def register_plugin(self, plugin: PermissionPlugin) -> None:
        self.register_plugins([plugin])

    def register_plugins(self, plugins: list[PermissionPlugin], *, skip_broken: bool = False) -> None:
        """拓扑注册一组插件，缺少依赖或循环依赖时拒绝启动。"""

        self._assert_mutable()
        pending = {plugin.manifest.plugin_id: plugin for plugin in plugins}
        if len(pending) != len(plugins):
            raise ValueError("duplicate permission plugin id")
        while pending:
            missing = {
                plugin_id: sorted(set(plugin.manifest.requires) - self._plugins.keys() - pending.keys())
                for plugin_id, plugin in pending.items()
            }
            missing = {plugin_id: requires for plugin_id, requires in missing.items() if requires}
            if missing:
                if not skip_broken:
                    details = "; ".join(f"{key}: {', '.join(value)}" for key, value in sorted(missing.items()))
                    raise ValueError(f"permission plugin dependencies are missing: {details}")
                for plugin_id, requires in missing.items():
                    logger.error(
                        "跳过权限插件 %s：缺少依赖 %s",
                        plugin_id,
                        ", ".join(requires),
                    )
                    pending.pop(plugin_id)
                continue
            ready = [
                plugin
                for plugin in pending.values()
                if set(plugin.manifest.requires).issubset(self._plugins)
            ]
            if not ready:
                unresolved = ", ".join(sorted(pending))
                if skip_broken:
                    logger.error("跳过存在循环依赖的权限插件：%s", unresolved)
                    break
                raise ValueError(f"permission plugin dependencies are missing or cyclic: {unresolved}")
            for plugin in sorted(ready, key=lambda item: item.manifest.plugin_id):
                manifest = plugin.manifest
                if manifest.plugin_id in self._plugins:
                    raise ValueError(f"duplicate permission plugin: {manifest.plugin_id}")
                self._validate_plugin_id(manifest.plugin_id)
                self._plugins[manifest.plugin_id] = manifest
                self._registering_plugin = manifest.plugin_id
                previous_nodes = self._nodes.copy()
                previous_groups = self._groups.copy()
                previous_levels = self._levels.copy()
                try:
                    plugin.register(self)
                except Exception:
                    self._plugins.pop(manifest.plugin_id, None)
                    self._nodes = previous_nodes
                    self._groups = previous_groups
                    self._levels = previous_levels
                    if not skip_broken:
                        raise
                    logger.exception("权限插件 %s 注册失败，已按默认拒绝跳过", manifest.plugin_id)
                finally:
                    self._registering_plugin = None
                pending.pop(manifest.plugin_id)

    def freeze(self) -> PermissionRegistry:
        self._frozen = True
        return self

    def group(self, key: str) -> PermissionGroup | None:
        return self._groups.get(key)

    def node(self, key: str) -> PermissionNode | None:
        return self._nodes.get(key)

    def level(self, value: int) -> PermissionLevel | None:
        return self._levels.get(value)

    def _assert_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("permission registry is frozen")

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key.startswith("webui.") or any(char.isspace() for char in key):
            raise ValueError(f"permission keys must use the webui namespace: {key}")

    @staticmethod
    def _validate_plugin_id(plugin_id: str) -> None:
        if not plugin_id.startswith("webui.") or any(char.isspace() for char in plugin_id):
            raise ValueError(f"permission plugin ids must use the webui namespace: {plugin_id}")


def _external_plugins() -> list[PermissionPlugin]:
    """加载部署环境安装的权限插件入口点。"""

    discovered: list[PermissionPlugin] = []
    for candidate in entry_points(group="medrag_nexus.webui_permission_plugins"):
        try:
            loaded = candidate.load()
            plugin = loaded() if isinstance(loaded, type) else loaded
            discovered.append(plugin)
        except Exception:
            logger.exception("权限插件入口点 %s 加载失败，已按默认拒绝跳过", candidate.name)
    return discovered


def build_default_registry() -> PermissionRegistry:
    from .plugins.core_accounts import CoreAccountsPlugin
    from .plugins.knowledge import KnowledgePlugin

    registry = PermissionRegistry()
    registry.register_plugins([CoreAccountsPlugin(), KnowledgePlugin()])
    registry.register_plugins(_external_plugins(), skip_broken=True)
    all_nodes = {node.key for node in registry.nodes}
    registered = {
        "webui.chat.use",
        "webui.retrieval.use",
        "webui.system.read",
        "webui.account.update_self",
        "webui.account.password.change_self",
        "webui.permission.catalog.read",
        "webui.user.read",
        "webui.workspace.read",
        "webui.resource.file.download",
        "webui.agent.export",
    }
    editor = registered | {
        "webui.resource.file.add",
        "webui.resource.file.delete",
        "webui.resource.text.add",
        "webui.resource.text.delete",
    }
    manager = editor | {
        "webui.workspace.create",
        "webui.workspace.rename",
        "webui.workspace.delete",
        "webui.workspace.policy.manage",
    }
    registry.set_level_permissions(0, registered)
    registry.set_level_permissions(1, editor)
    registry.set_level_permissions(2, manager)
    registry.set_level_permissions(1000, all_nodes)
    return registry.freeze()


class PermissionEngine:
    """组合权限节点、等级阈值与显式 ACL 决策。"""

    def __init__(self, registry: PermissionRegistry):
        self.registry = registry

    def allows(self, permission: str, effective_permissions: set[str] | frozenset[str]) -> bool:
        node = self.registry.node(permission)
        return bool(node and node.available and permission in effective_permissions)

    def allows_resource(
        self,
        permission: str,
        effective_permissions: set[str] | frozenset[str],
        *,
        account_level: int,
        minimum_level: int,
        acl_effect: str | None = None,
    ) -> bool:
        if not self.allows(permission, effective_permissions) or account_level < minimum_level:
            return False
        if acl_effect == "deny":
            return False
        return acl_effect == "allow"
