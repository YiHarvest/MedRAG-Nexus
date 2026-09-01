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
        document = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            swagger_ui_parameters=app.swagger_ui_parameters,
        )
        html = (
            document.body.decode("utf-8")
            .replace(
                "<!-- `SwaggerUIBundle` is now available on the page -->",
                """<script>
    const HideEmptyValuePlugin = () => ({
        components: { ParameterIncludeEmpty: () => null },
    })
    const SanitizeAddRequest = (request) => {
        if (!request.url.includes("/api/v1/add") || !(request.body instanceof FormData)) {
            return request
        }
        const sourceType = request.body.get("type")
        if (sourceType === "file") request.body.delete("content")
        if (sourceType === "str") request.body.delete("file")
        return request
    }
    </script>
    <!-- `SwaggerUIBundle` is now available on the page -->""",
            )
            .replace(
                "    presets: [",
                """    plugins: [HideEmptyValuePlugin],
    requestInterceptor: SanitizeAddRequest,
    showMutatedRequest: true,
    presets: [""",
            )
        )
        return HTMLResponse(html)


__all__ = ["install_documentation_routes"]
