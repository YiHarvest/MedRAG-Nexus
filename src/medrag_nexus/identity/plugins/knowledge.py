"""知识域、知识库、资源和单 Agent 权限节点。"""

from __future__ import annotations

from ..permissions import PermissionRegistry, PluginManifest


class KnowledgePlugin:
    manifest = PluginManifest(
        plugin_id="webui.knowledge",
        version="2.0.0",
        requires=("webui.core_accounts",),
    )

    def register(self, registry: PermissionRegistry) -> None:
        descriptions = {
            "webui.chat.use": "使用按权限过滤的 WebUI Agent",
            "webui.retrieval.use": "使用按权限过滤的文档检索",
            "webui.system.read": "查看 WebUI 系统状态",
            "webui.user.read": "查看已授权知识域",
            "webui.user.create": "创建知识域",
            "webui.user.rename": "修改知识域名称",
            "webui.user.delete": "删除知识域及其全部知识库",
            "webui.user.binding.manage": "绑定普通账号与知识域",
            "webui.user.policy.manage": "管理知识域权限",
            "webui.workspace.read": "查看已授权知识库",
            "webui.workspace.create": "在已授权知识域下创建知识库",
            "webui.workspace.rename": "重命名已授权知识库",
            "webui.workspace.delete": "删除已授权知识库",
            "webui.workspace.policy.manage": "管理知识库策略",
            "webui.resource.file.add": "上传完整文件",
            "webui.resource.file.download": "下载完整文件",
            "webui.resource.file.delete": "删除完整文件",
            "webui.resource.text.add": "添加完整文本",
            "webui.resource.text.delete": "删除完整文本",
            "webui.agent.export": "将 Agent 回答导出为临时文件",
        }
        protected = {
            "webui.user.binding.manage",
        }
        for key, description in descriptions.items():
            registry.register_node(key, description, custom_assignable=key not in protected)
