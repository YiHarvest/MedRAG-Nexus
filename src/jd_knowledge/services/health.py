"""检查服务运行状态及外部依赖的可用性。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from jd_knowledge.core.models import DependencyState, HealthResponse
from jd_knowledge.services.runtime import Runtime


async def _check(call: Callable[[], Awaitable[object]], timeout_seconds: float) -> DependencyState:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(call(), timeout=timeout_seconds)
        return DependencyState(status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))
    except Exception as exc:
        return DependencyState(
            status="unavailable",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=f"{type(exc).__name__}: dependency check failed",
        )


async def dependency_health(runtime: Runtime) -> HealthResponse:
    async def worker() -> None:
        if not await runtime.tasks.worker_alive():
            raise RuntimeError("no worker heartbeat")

    async def files() -> None:
        await runtime.artifacts.ensure()

    async def mineru() -> None:
        if not runtime.settings.mineru_url:
            raise RuntimeError("not configured")
        async with httpx.AsyncClient(
            timeout=runtime.settings.dependency_timeout_seconds,
            trust_env=False,
        ) as client:
            base = runtime.settings.mineru_url.rstrip("/")
            response = await client.get(f"{base}/health")
            response.raise_for_status()
            if runtime.settings.mineru_uses_openai_server:
                models = await client.get(f"{base}/v1/models")
                models.raise_for_status()
                rows = models.json().get("data", [])
                if not rows:
                    raise RuntimeError("MinerU OpenAI server exposes no models")
                return
            endpoint = f"{base}/{runtime.settings.mineru_api_path.lstrip('/')}"
            probe = await client.post(endpoint)
            if probe.status_code == 404 or probe.status_code >= 500:
                raise RuntimeError("MinerU parse endpoint is unavailable")

    checks = {
        "redis": runtime.tasks.health,
        "worker": worker,
        "sqlite": runtime.metadata.health,
        "elasticsearch": runtime.elasticsearch.health,
        "milvus": runtime.milvus.health,
        "embedding": runtime.embedding.health,
        "filesystem": files,
        "mineru": mineru,
        "rerank": runtime.rerank.health,
    }
    names = list(checks)
    states = await asyncio.gather(
        *[_check(checks[name], runtime.settings.dependency_timeout_seconds) for name in names]
    )
    dependencies = dict(zip(names, states, strict=True))
    optional = {"mineru", "rerank"}
    core_failed = any(state.status == "unavailable" for name, state in dependencies.items() if name not in optional)
    optional_failed = any(state.status == "unavailable" for name, state in dependencies.items() if name in optional)
    status = "unavailable" if core_failed else "degraded" if optional_failed else "ok"
    for name in optional:
        if dependencies[name].status == "unavailable":
            dependencies[name].status = "degraded"
    return HealthResponse(status=status, dependencies=dependencies)


async def readiness(runtime: Runtime) -> HealthResponse:
    result = await dependency_health(runtime)
    return HealthResponse(status="unavailable" if result.status == "unavailable" else result.status)
