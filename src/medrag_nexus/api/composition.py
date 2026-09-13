"""Typed application services and composable feature lifecycle contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI

from medrag_nexus.core.config import Settings, get_settings
from medrag_nexus.mcp import bind_runtime
from medrag_nexus.services.runtime import Runtime


class ApplicationFeature(Protocol):
    """One independently installable and startable application feature."""

    def install(self, app: FastAPI) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    """Typed dependencies shared by API features."""

    runtime: Runtime
    backend_runtime: Runtime
    settings: Settings
    owns_runtime: bool = False
    owns_backend_runtime: bool = False

    @classmethod
    def build(
        cls,
        runtime: Runtime | None = None,
        *,
        backend_runtime: Runtime | None = None,
    ) -> ServiceContainer:
        settings = get_settings()
        selected_runtime = runtime or Runtime(settings)
        selected_settings = getattr(selected_runtime, "settings", settings)
        selected_backend = backend_runtime or (
            selected_runtime if runtime is not None else Runtime(settings.backend_runtime_settings())
        )
        return cls(
            runtime=selected_runtime,
            backend_runtime=selected_backend,
            settings=selected_settings,
            owns_runtime=runtime is None,
            owns_backend_runtime=backend_runtime is None and selected_backend is not selected_runtime,
        )


class RuntimeFeature:
    """Own the runtime instances created by the default service container."""

    def __init__(self, services: ServiceContainer):
        self.services = services

    def install(self, app: FastAPI) -> None:
        app.state.services = self.services
        app.state.runtime = self.services.runtime
        app.state.backend_runtime = self.services.backend_runtime

    async def start(self) -> None:
        if self.services.owns_runtime:
            await self.services.runtime.start()
        if self.services.owns_backend_runtime:
            await self.services.backend_runtime.start()
        bind_runtime(self.services.runtime)

    async def close(self) -> None:
        if self.services.owns_backend_runtime:
            await self.services.backend_runtime.close()
        if self.services.owns_runtime:
            await self.services.runtime.close()


class FeatureLifecycle:
    """Install features in order, and close successfully started features in reverse."""

    def __init__(self, features: Iterable[ApplicationFeature]):
        self.features = tuple(features)
        self._started: list[ApplicationFeature] = []

    def install(self, app: FastAPI) -> None:
        for feature in self.features:
            feature.install(app)

    async def start(self) -> None:
        try:
            for feature in self.features:
                # Register before starting so a partially-started feature is also rolled back.
                self._started.append(feature)
                await feature.start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        while self._started:
            await self._started.pop().close()


__all__ = ["ApplicationFeature", "FeatureLifecycle", "RuntimeFeature", "ServiceContainer"]
