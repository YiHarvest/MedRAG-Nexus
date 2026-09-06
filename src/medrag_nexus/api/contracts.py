"""后端 API 共用的 OpenAPI 元数据。"""

from medrag_nexus.core.models import HealthResponse

OPENAPI_TAGS = [
    {
        "name": "认证与账号",
        "description": "后端负责注册、登录、账号、密码、权限组与审计。",
    },
    {
        "name": "知识域与知识库",
        "description": "受 Session、权限与 ACL 保护的用户、知识域、文件、检索、聊天与任务接口。",
    },
    {
        "name": "Agent",
        "description": "受认证保护的 Agent 动作与审批接口。",
    },
    {
        "name": "健康检查",
        "description": "供基础设施探测的公开存活和就绪检查。",
    },
]

HEALTH_UNAVAILABLE = {
    "model": HealthResponse,
    "description": "一个或多个核心依赖不可用。",
}

__all__ = ["HEALTH_UNAVAILABLE", "OPENAPI_TAGS"]
