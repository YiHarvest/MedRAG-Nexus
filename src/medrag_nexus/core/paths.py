"""后端路由与中间件共用的标准 HTTP API 路径。"""

API_V1_PREFIX = "/api/v1"
AGENT_API_PREFIX = f"{API_V1_PREFIX}/agent"
HEALTH_API_PREFIX = f"{API_V1_PREFIX}/health"

__all__ = ["AGENT_API_PREFIX", "API_V1_PREFIX", "HEALTH_API_PREFIX"]
