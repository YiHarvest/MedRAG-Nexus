"""Canonical HTTP API paths shared by backend routers and middleware."""

API_V1_PREFIX = "/api/v1"
AGENT_API_PREFIX = f"{API_V1_PREFIX}/agent"
HEALTH_API_PREFIX = f"{API_V1_PREFIX}/health"

__all__ = ["AGENT_API_PREFIX", "API_V1_PREFIX", "HEALTH_API_PREFIX"]
