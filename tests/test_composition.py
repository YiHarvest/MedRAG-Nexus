"""Verify typed service composition and feature lifecycle ordering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from medrag_nexus.api.application import create_app
from medrag_nexus.api.composition import FeatureLifecycle, ServiceContainer


class RecordingFeature:
    def __init__(self, name: str, events: list[str], *, fail_start: bool = False):
        self.name = name
        self.events = events
        self.fail_start = fail_start

    def install(self, app: FastAPI) -> None:
        self.events.append(f"install:{self.name}")
        app.state.installed = [*getattr(app.state, "installed", []), self.name]

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError(self.name)

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")


async def test_feature_lifecycle_installs_in_order_and_closes_in_reverse() -> None:
    events: list[str] = []
    lifecycle = FeatureLifecycle([RecordingFeature("one", events), RecordingFeature("two", events)])
    app = FastAPI()

    lifecycle.install(app)
    await lifecycle.start()
    await lifecycle.close()

    assert events == ["install:one", "install:two", "start:one", "start:two", "close:two", "close:one"]
    assert app.state.installed == ["one", "two"]


async def test_feature_lifecycle_rolls_back_started_features() -> None:
    events: list[str] = []
    lifecycle = FeatureLifecycle(
        [RecordingFeature("one", events), RecordingFeature("two", events, fail_start=True)]
    )

    with pytest.raises(RuntimeError, match="two"):
        await lifecycle.start()

    assert events == ["start:one", "start:two", "close:two", "close:one"]


def test_create_app_accepts_typed_services_and_custom_features() -> None:
    runtime = SimpleNamespace()
    settings = SimpleNamespace(
        max_file_bytes=1024,
        sqlite_path=__import__("pathlib").Path("/tmp/composition.sqlite3"),
        webui_data_root=__import__("pathlib").Path("/tmp/composition"),
        webui_cookie_secure=False,
        webui_lock_password="",
    )
    services = ServiceContainer(runtime=runtime, backend_runtime=runtime, settings=settings)  # type: ignore[arg-type]
    events: list[str] = []

    app = create_app(services=services, features=[RecordingFeature("custom", events)])

    assert app.state.services is services
    assert app.state.runtime is runtime
    assert events == ["install:custom"]


def test_create_app_rejects_ambiguous_dependency_inputs() -> None:
    runtime = SimpleNamespace()
    services = ServiceContainer(runtime=runtime, backend_runtime=runtime, settings=SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="cannot be combined"):
        create_app(runtime, services=services)  # type: ignore[arg-type]
