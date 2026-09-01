"""验证统一 HTTP API 路由、认证边界与基础设施端点。"""

from __future__ import annotations

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from medrag_nexus.api import health as health_api
from medrag_nexus.api.main import create_app
from medrag_nexus.core.models import DependencyState, HealthResponse


class FakeRuntime:
    pass


class CapturingTaskLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def write_api(self, level: str, message: str, **context: object) -> None:
        self.events.append((level, message, context))


class CapturingRuntime:
    def __init__(self) -> None:
        self.task_log = CapturingTaskLog()


async def test_live_endpoint_and_canonical_api_tree() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")
        legacy = await client.get("/api/webui/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dependencies": None}
    assert legacy.status_code == 404

    paths = app.openapi()["paths"]
    expected_paths = {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/users",
        "/api/v1/workspaces",
        "/api/v1/workspaces/{workspace_id}/resources",
        "/api/v1/retrieval",
        "/api/v1/chat/stream",
        "/api/v1/tasks/{task_id}",
        "/api/v1/agent/actions/{action_id}",
        "/api/v1/health/live",
        "/api/v1/health/ready",
    }
    assert expected_paths <= set(paths)
    assert all(not path.startswith("/api/webui/") for path in paths)
    assert "/api/v1/add" not in paths
    assert "/api/v1/delete" not in paths
    assert "/api/v1/delete-string" not in paths


def test_api_has_no_duplicate_method_path_pairs() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.extend(item for item in original_router.routes if isinstance(item, APIRoute))
    pairs = [(method, route.path) for route in routes for method in route.methods]
    assert len({route.path for route in routes if route.path.startswith("/api/v1")}) == 41
    assert len(pairs) == len(set(pairs))


def test_openapi_describes_backend_owned_resources() -> None:
    schema = create_app(FakeRuntime()).openapi()  # type: ignore[arg-type]
    assert schema["info"]["title"] == "MedRAG-Nexus 知识库服务"
    assert [tag["name"] for tag in schema["tags"]] == [
        "认证与账号",
        "知识域与知识库",
        "Agent",
        "健康检查",
    ]

    user_schema = schema["components"]["schemas"]["CreateKnowledgeUserRequest"]
    workspace_schema = schema["components"]["schemas"]["CreateWorkspaceRequest"]
    assert "user_id" not in user_schema.get("required", [])
    assert "user_id" in workspace_schema["required"]
    assert "workspace_id" not in workspace_schema["properties"]


async def test_root_and_plain_swagger_documentation() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        root = await client.get("/")
        docs = await client.get("/docs")

    assert root.status_code == 200
    assert root.json()["service"] == "medrag-nexus"
    assert root.json()["health"] == "/api/v1/health/live"
    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text
    assert "SanitizeAddRequest" not in docs.text


async def test_business_routes_require_a_backend_session() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        workspace_list = await client.get("/api/v1/workspaces")
        user_create = await client.post("/api/v1/users", json={"user_name": "未认证知识域"})
        retrieval = await client.post(
            "/api/v1/retrieval",
            json={"workspace_id": "workspace_missing", "query": "test", "top_k": 5},
        )

    assert workspace_list.status_code == 401
    assert user_create.status_code == 401
    assert retrieval.status_code == 401


async def test_ready_details_uses_the_public_health_endpoint(monkeypatch) -> None:
    calls = {"readiness": 0, "dependencies": 0}

    async def fake_readiness(runtime):
        calls["readiness"] += 1
        return HealthResponse(status="ok")

    async def fake_dependency_health(runtime):
        calls["dependencies"] += 1
        return HealthResponse(
            status="degraded",
            dependencies={"mineru": DependencyState(status="degraded", latency_ms=12.5)},
        )

    monkeypatch.setattr(health_api, "readiness", fake_readiness)
    monkeypatch.setattr(health_api, "dependency_health", fake_dependency_health)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        summary = await client.get("/api/v1/health/ready")
        detailed = await client.get("/api/v1/health/ready", params={"details": "true"})

    assert summary.status_code == 200
    assert summary.json() == {"status": "ok", "dependencies": None}
    assert detailed.status_code == 200
    assert detailed.json()["dependencies"]["mineru"]["latency_ms"] == 12.5
    assert calls == {"readiness": 1, "dependencies": 1}


async def test_http_requests_are_logged() -> None:
    runtime = CapturingRuntime()
    app = create_app(runtime)  # type: ignore[arg-type]
    app.state.runtime = runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    messages = [message for _, message, _ in runtime.task_log.events]
    assert messages == ["请求开始", "请求完成"]
    assert all(context["request_id"] for _, _, context in runtime.task_log.events)
