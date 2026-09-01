"""装配 FastAPI 应用及其生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from medrag_nexus.core.config import get_settings
from medrag_nexus.mcp import bind_runtime, mcp_http_app
from medrag_nexus.services.runtime import Runtime

from .contracts import OPENAPI_TAGS
from .docs import install_documentation_routes
from .health import create_health_router
from .http import install_http_infrastructure
from .lifecycle import ApplicationLifecycle

_DESCRIPTION = """
所有 REST 业务接口使用 `/api/v1` 前缀，并由后端账号 Session、权限与资源 ACL 保护。

- 账号、知识域与 Workspace 均通过后端接口创建并持久化。
- 文件与字符串写入、删除采用异步任务；检索和列表同步返回。
- 注册和登录负责建立 Session；其余业务接口要求 Session、权限与 ACL。
- `/api/v1/health/*` 是无需账号身份的公开基础设施探针。
"""


def create_app(
    runtime: Runtime | None = None,
    *,
    backend_runtime: Runtime | None = None,
) -> FastAPI:
    settings = get_settings()
    selected_runtime = runtime or Runtime(settings)
    selected_settings = getattr(selected_runtime, "settings", settings)
    selected_backend_runtime = backend_runtime or (
        selected_runtime if runtime is not None else Runtime(settings.backend_runtime_settings())
    )
    lifecycle = ApplicationLifecycle(selected_backend_runtime, selected_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = selected_runtime
        app.state.backend_runtime = selected_backend_runtime
        if runtime is None:
            await selected_runtime.start()
        if selected_backend_runtime is not selected_runtime and backend_runtime is None:
            await selected_backend_runtime.start()
        await lifecycle.start()
        bind_runtime(selected_runtime)
        try:
            async with mcp_http_app.router.lifespan_context(mcp_http_app):
                yield
        finally:
            await lifecycle.close()
            if selected_backend_runtime is not selected_runtime and backend_runtime is None:
                await selected_backend_runtime.close()
            if runtime is None:
                await selected_runtime.close()

    app = FastAPI(
        title="MedRAG-Nexus 知识库服务",
        summary="基于 workspace 的异步知识入库与混合召回 API",
        version="0.3.0",
        description=_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url=None,
    )
    lifecycle.install(app)
    install_http_infrastructure(app, max_file_bytes=settings.max_file_bytes)
    install_documentation_routes(app)
    app.include_router(create_health_router())
    app.mount("/", mcp_http_app)
    return app


__all__ = ["create_app"]
