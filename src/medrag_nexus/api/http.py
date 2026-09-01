"""HTTP 请求上下文、日志与异常处理。"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from medrag_nexus.core.models import DomainError, ErrorBody, ErrorResponse
from medrag_nexus.services.runtime import Runtime


def runtime_from(request: Request) -> Runtime:
    return request.app.state.runtime


async def api_log(request: Request, level: str, message: str, **context: object) -> None:
    """安全写入 API 日志，日志设施异常不能影响接口响应。"""
    runtime = getattr(request.app.state, "runtime", None)
    task_log = getattr(runtime, "task_log", None) if runtime is not None else None
    if task_log is None:
        return
    try:
        await task_log.write_api(
            level,
            message,
            request_id=getattr(request.state, "request_id", "unknown"),
            **context,
        )
    except Exception:
        return


def error_body(
    request: Request,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", "unknown"),
            details=details,
        )
    ).model_dump(mode="json", exclude_none=True)


def install_http_infrastructure(app: FastAPI, *, max_file_bytes: int) -> None:
    """安装所有 HTTP 路由共用的中间件和异常处理器。"""

    @app.middleware("http")
    async def log_api_requests(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        client = request.client.host if request.client else "unknown"
        common_context = {
            "method": request.method,
            "path": request.url.path,
            "client": client,
            "content_type": request.headers.get("content-type", "-").partition(";")[0],
            "content_length": request.headers.get("content-length", "unknown"),
            "query_fields": ",".join(sorted(set(request.query_params.keys()))) or "-",
        }
        await api_log(request, "INFO", "请求开始", **common_context)
        try:
            response = await call_next(request)
            level = "INFO" if response.status_code < 400 else "WARN" if response.status_code < 500 else "ERROR"
            await api_log(
                request,
                level,
                "请求完成",
                status=response.status_code,
                response_length=response.headers.get("content-length", "unknown"),
                elapsed_ms=round((time.monotonic() - started) * 1000),
                **common_context,
            )
            return response
        except Exception as exc:
            await api_log(
                request,
                "ERROR",
                "请求处理异常",
                exception_type=type(exc).__name__,
                error=str(exc)[:500],
                elapsed_ms=round((time.monotonic() - started) * 1000),
                **common_context,
            )
            raise

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        content_length = request.headers.get("content-length")
        max_request_bytes = max_file_bytes + 1024 * 1024
        if content_length and content_length.isdigit() and int(content_length) > max_request_bytes:
            await api_log(
                request,
                "WARN",
                "请求体超过大小限制",
                method=request.method,
                path=request.url.path,
                content_length=content_length,
                max_request_bytes=max_request_bytes,
            )
            return JSONResponse(
                status_code=413,
                content=error_body(request, "payload_too_large", "request body exceeds the configured limit"),
                headers={"X-Request-ID": request.state.request_id},
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        await api_log(
            request,
            "WARN" if exc.status_code < 500 else "ERROR",
            "业务校验失败",
            method=request.method,
            path=request.url.path,
            status=exc.status_code,
            error_code=exc.code,
            error=exc.message[:500],
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, exc.code, exc.message, details=exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = json.loads(json.dumps(exc.errors(), default=str))
        await api_log(
            request,
            "WARN",
            "请求参数校验失败",
            method=request.method,
            path=request.url.path,
            status=422,
            error_count=len(errors),
        )
        return JSONResponse(
            status_code=422,
            content=error_body(
                request,
                "validation_error",
                "request validation failed",
                details={"errors": errors},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        await api_log(
            request,
            "ERROR",
            "未处理的接口异常",
            method=request.method,
            path=request.url.path,
            status=500,
            exception_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        return JSONResponse(
            status_code=500,
            content=error_body(request, "internal_error", "an unexpected internal error occurred"),
        )


__all__ = ["api_log", "error_body", "install_http_infrastructure", "runtime_from"]
