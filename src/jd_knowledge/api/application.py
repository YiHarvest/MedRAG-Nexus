"""装配 FastAPI 应用及其生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType

from fastapi import FastAPI

from jd_knowledge.core.config import get_settings
from jd_knowledge.mcp import bind_runtime, mcp_http_app
from jd_knowledge.services.runtime import Runtime
from jd_knowledge.webui import WebUiFeature

from . import services as default_services
from .contracts import OPENAPI_TAGS
from .docs import install_documentation_routes
from .http import install_http_infrastructure
from .routes import create_public_router

_DESCRIPTION = """
所有业务接口使用 `/api/v1` 前缀。当前版本不包含鉴权。

- `user_id`、`workspace_id`、`workspace_name` 均由前端提供；服务不会自动生成 Workspace ID。
- 文件首次创建时生成永久 `file_<UUID4>`；仅支持 PDF、TXT、DOCX。
- `type=str` 的原始字符串追加到 workspace JSONL，并按规范化内容 SHA-256 去重。
- 新增与删除异步执行，使用任务接口轮询，或传入 callback_url 接收状态、进度与结果。
- Workspace 列表、资源列表与检索同步执行并直接返回结果。
"""


def create_app(
    runtime: Runtime | None = None,
    *,
    webui_runtime: Runtime | None = None,
    services: ModuleType = default_services,
) -> FastAPI:
    settings = get_settings()
    selected_runtime = runtime or Runtime(settings)
    selected_settings = getattr(selected_runtime, "settings", settings)
    selected_webui_runtime = webui_runtime or (
        selected_runtime if runtime is not None else Runtime(settings.webui_runtime_settings())
    )
    webui = WebUiFeature(selected_webui_runtime, selected_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = selected_runtime
        app.state.webui_runtime = selected_webui_runtime
        if runtime is None:
            await selected_runtime.start()
        if selected_webui_runtime is not selected_runtime and webui_runtime is None:
            await selected_webui_runtime.start()
        await webui.start()
        bind_runtime(selected_runtime)
        try:
            async with mcp_http_app.router.lifespan_context(mcp_http_app):
                yield
        finally:
            await webui.close()
            if selected_webui_runtime is not selected_runtime and webui_runtime is None:
                await selected_webui_runtime.close()
            if runtime is None:
                await selected_runtime.close()

    app = FastAPI(
        title="JD Knowledge 知识库服务",
        summary="基于 workspace 的异步知识入库与混合召回 API",
        version="0.3.0",
        description=_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url=None,
    )
    webui.install(app)
    install_http_infrastructure(app, max_file_bytes=settings.max_file_bytes)
    install_documentation_routes(app)
    app.include_router(create_public_router(max_file_bytes=settings.max_file_bytes, services=services))
    app.mount("/", mcp_http_app)
    return app


__all__ = ["create_app"]
