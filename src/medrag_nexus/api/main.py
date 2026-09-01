"""Uvicorn 启动入口，应用逻辑按职责放在各个 API 模块中。"""

from __future__ import annotations

import sys

import uvicorn

from medrag_nexus.core.config import get_settings

from .application import create_app as _create_app
from .services import FileService, dependency_health, readiness, retrieve, stream_chat


def create_app(runtime=None, *, webui_runtime=None):
    """兼容原有应用工厂入口，并支持测试时替换服务依赖。"""
    return _create_app(runtime, webui_runtime=webui_runtime, services=sys.modules[__name__])


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
    "FileService",
    "app",
    "create_app",
    "dependency_health",
    "readiness",
    "retrieve",
    "run",
    "stream_chat",
]
