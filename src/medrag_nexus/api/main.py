"""Uvicorn 启动入口，应用逻辑按职责放在各个 API 模块中。"""

from __future__ import annotations

import uvicorn

from medrag_nexus.core.config import get_settings

from .application import create_app as _create_app


def create_app(runtime=None, *, backend_runtime=None):
    """创建统一的后端 HTTP API、Worker 与 MCP 应用。"""
    return _create_app(runtime, backend_runtime=backend_runtime)


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "medrag_nexus.api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level,
    )


__all__ = [
    "app",
    "create_app",
    "run",
]
