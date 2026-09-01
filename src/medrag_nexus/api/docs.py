"""服务首页与自定义 Swagger UI。"""

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse


def install_documentation_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "medrag-nexus",
                "version": "0.3.0",
                "docs": "/docs",
                "mcp": "/mcp",
                "health": "/api/v1/health/live",
            }
        )

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            swagger_ui_parameters=app.swagger_ui_parameters,
        )


__all__ = ["install_documentation_routes"]
