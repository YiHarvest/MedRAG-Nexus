"""无需认证的基础设施健康检查。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from medrag_nexus.core.models import HealthResponse
from medrag_nexus.core.paths import HEALTH_API_PREFIX
from medrag_nexus.services.health import dependency_health, readiness

from .contracts import HEALTH_UNAVAILABLE
from .http import api_log, runtime_from


def create_health_router() -> APIRouter:
    router = APIRouter(prefix=HEALTH_API_PREFIX, tags=["健康检查"])

    @router.get("/live", response_model=HealthResponse, summary="进程存活检查")
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @router.get(
        "/ready",
        response_model=HealthResponse,
        summary="服务就绪检查",
        responses={503: HEALTH_UNAVAILABLE},
    )
    async def ready(
        request: Request,
        details: Annotated[bool, Query(description="是否返回脱敏依赖详情。")] = False,
    ) -> JSONResponse | HealthResponse:
        response = await dependency_health(runtime_from(request)) if details else await readiness(runtime_from(request))
        await api_log(
            request,
            "INFO" if response.status == "ok" else "WARN",
            "服务就绪检查完成",
            status=response.status,
            details=details,
            dependency_count=len(response.dependencies or {}),
            unavailable_dependencies=",".join(
                name for name, state in (response.dependencies or {}).items() if state.status == "unavailable"
            )
            or "-",
        )
        if response.status == "unavailable":
            return JSONResponse(status_code=503, content=response.model_dump(mode="json", exclude_none=True))
        return response

    return router


__all__ = ["create_health_router"]
