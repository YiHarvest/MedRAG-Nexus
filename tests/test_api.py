"""验证 HTTP API 路由、契约、日志与请求转换。"""

from __future__ import annotations

import base64

from httpx import ASGITransport, AsyncClient

from jd_knowledge.api import main as api_main
from jd_knowledge.api.main import create_app
from jd_knowledge.core.models import (
    DeleteStringRequest,
    DependencyState,
    FileListResponse,
    FileSource,
    HealthResponse,
    StringListItem,
    StringSource,
    TaskAccepted,
    UserListItem,
    UserListResponse,
    WorkspaceStats,
)

WORKSPACE_ID = "workspace_11111111-1111-5111-8111-111111111111"


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


async def test_live_endpoint_and_new_api_prefix() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health/live")
        old = await client.get("/app/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dependencies": None}
    assert old.status_code == 404
    paths = app.openapi()["paths"]
    assert "/api/v1/add" in paths
    assert "/api/v1/users" in paths
    assert "/api/v1/users/{user_id}/workspaces" in paths
    assert "/api/v1/workspaces/{workspace_id}/files" in paths
    assert "/api/v1/delete" in paths
    assert "/api/v1/delete-string" in paths
    assert "/api/v1/retrieval" in paths
    assert "/api/v1/tasks/{task_id}" in paths
    assert all(not path.startswith("/app/v1") for path in paths)


async def test_root_returns_service_info() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "jd-knowledge"
    assert body["docs"] == "/docs"
    assert body["health"] == "/api/v1/health/live"


async def test_swagger_hides_send_empty_value_controls() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/docs")

    assert response.status_code == 200
    assert "ParameterIncludeEmpty: () => null" in response.text
    assert "plugins: [HideEmptyValuePlugin]" in response.text
    assert 'if (sourceType === "file") request.body.delete("content")' in response.text
    assert 'if (sourceType === "str") request.body.delete("file")' in response.text
    assert "requestInterceptor: SanitizeAddRequest" in response.text
    assert "showMutatedRequest: true" in response.text


def test_openapi_add_contract_is_multipart_file_or_str() -> None:
    schema = create_app(FakeRuntime()).openapi()  # type: ignore[arg-type]
    assert schema["info"]["title"] == "JD Knowledge 知识库服务"
    assert [tag["name"] for tag in schema["tags"]] == [
        "知识新增",
        "列表",
        "删除",
        "检索",
        "聊天",
        "任务",
        "健康检查",
    ]
    operation = schema["paths"]["/api/v1/add"]["post"]
    request_content = operation["requestBody"]["content"]
    assert set(request_content) == {"multipart/form-data"}
    multipart_schema = request_content["multipart/form-data"]["schema"]
    assert multipart_schema["properties"]["file"]["format"] == "binary"
    assert multipart_schema["properties"]["type"]["enum"] == ["file", "str"]
    assert multipart_schema["properties"]["callback_url"]["format"] == "uri"
    assert "path" not in multipart_schema["properties"]
    assert "workspace_id" in multipart_schema["required"]


def test_add_and_delete_are_async_while_reads_return_results_directly() -> None:
    schema = create_app(FakeRuntime()).openapi()  # type: ignore[arg-type]
    async_operations = [
        schema["paths"]["/api/v1/add"]["post"],
        schema["paths"]["/api/v1/delete"]["post"],
        schema["paths"]["/api/v1/delete-string"]["post"],
    ]
    sync_operations = [
        schema["paths"]["/api/v1/users"]["get"],
        schema["paths"]["/api/v1/users/{user_id}/workspaces"]["get"],
        schema["paths"]["/api/v1/workspaces/{workspace_id}/files"]["get"],
        schema["paths"]["/api/v1/retrieval"]["post"],
    ]
    assert all("202" in operation["responses"] for operation in async_operations)
    assert all(
        operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/TaskAccepted")
        for operation in async_operations
    )
    assert all("200" in operation["responses"] and "202" not in operation["responses"] for operation in sync_operations)
    assert all(
        not any(parameter.get("name") == "callback_url" for parameter in operation.get("parameters", []))
        for operation in sync_operations
    )
    retrieval_schema = schema["components"]["schemas"]["RetrievalRequest"]
    assert "callback_url" not in retrieval_schema["properties"]


async def test_user_directory_returns_basic_stats_without_authentication(monkeypatch) -> None:
    async def fake_list_users(service):
        return UserListResponse(
            users=[
                UserListItem(
                    user_id="user-001",
                    user_name="产品团队",
                    workspace_count=2,
                    resource_count=3,
                    file_count=1,
                    str_count=2,
                    total_size_bytes=4096,
                )
            ]
        )

    monkeypatch.setattr(api_main.FileService, "list_users", fake_list_users)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/users")

    assert response.status_code == 200
    assert response.json() == {
        "users": [
            {
                "user_id": "user-001",
                "user_name": "产品团队",
                "workspace_count": 2,
                "resource_count": 3,
                "file_count": 1,
                "str_count": 2,
                "total_size_bytes": 4096,
            }
        ]
    }

    operation = app.openapi()["paths"]["/api/v1/users"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/UserListResponse"
    )
    assert "security" not in operation
    assert "parameters" not in operation


async def test_create_user_accepts_frontend_generated_id(monkeypatch) -> None:
    async def fake_create_user(service, payload):
        return UserListItem(user_id=payload.user_id, user_name=payload.user_name)

    monkeypatch.setattr(api_main.FileService, "create_user", fake_create_user)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/users",
            json={"user_id": "user_12345678-1234-4234-8234-123456789abc", "user_name": "张三"},
        )

    assert response.status_code == 201
    assert response.json() == {
        "user_id": "user_12345678-1234-4234-8234-123456789abc",
        "user_name": "张三",
        "workspace_count": 0,
        "resource_count": 0,
        "file_count": 0,
        "str_count": 0,
        "total_size_bytes": 0,
    }


async def test_ready_details_reuses_the_same_health_endpoint(monkeypatch) -> None:
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

    monkeypatch.setattr(api_main, "readiness", fake_readiness)
    monkeypatch.setattr(api_main, "dependency_health", fake_dependency_health)
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


async def test_add_rejects_json_and_returns_structured_error() -> None:
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            json={"user_id": "u", "workspace_name": "w", "type": "str", "content": "value"},
        )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert response.headers["X-Request-ID"]


async def test_multipart_file_is_normalized_to_add_request(monkeypatch) -> None:
    submitted = []

    async def fake_submit_add(service, payload):
        submitted.append(payload)
        return TaskAccepted(task_id="a" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_add", fake_submit_add)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            data={
                "user_id": "user-001",
                "workspace_id": "workspace_1",
                "workspace_name": "产品知识库",
                "type": "file",
            },
            files={"file": ("manual.pdf", b"%PDF-1.7\nexample", "application/pdf")},
        )

    assert response.status_code == 202
    payload = submitted[0]
    assert isinstance(payload.source, FileSource)
    assert payload.workspace_name == "产品知识库"
    assert payload.source.file_name == "manual.pdf"
    assert base64.b64decode(payload.source.content_base64) == b"%PDF-1.7\nexample"


async def test_all_http_requests_and_upload_progress_are_logged(monkeypatch) -> None:
    async def fake_submit_add(service, payload):
        return TaskAccepted(task_id="d" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_add", fake_submit_add)
    runtime = CapturingRuntime()
    app = create_app(runtime)  # type: ignore[arg-type]
    app.state.runtime = runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            data={
                "user_id": "user-001",
                "workspace_id": "workspace_1",
                "workspace_name": "产品知识库",
                "type": "file",
            },
            files={"file": ("manual.pdf", b"%PDF-1.7\nexample", "application/pdf")},
        )

    assert response.status_code == 202
    messages = [message for _, message, _ in runtime.task_log.events]
    assert messages[0] == "请求开始"
    assert "上传表单解析完成" in messages
    assert "开始读取上传文件" in messages
    assert "上传文件读取进度" in messages
    assert "上传文件读取完成" in messages
    assert messages[-1] == "请求完成"
    assert all(context["request_id"] for _, _, context in runtime.task_log.events)


async def test_multipart_file_ignores_swagger_empty_content_field(monkeypatch) -> None:
    submitted = []

    async def fake_submit_add(service, payload):
        submitted.append(payload)
        return TaskAccepted(task_id="c" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_add", fake_submit_add)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            data={
                "user_id": "user-001",
                "workspace_id": "workspace_1",
                "workspace_name": "产品知识库",
                "type": "file",
                "content": "",
            },
            files={"file": ("主大脑提示词.txt", b"prompt", "text/plain")},
        )

    assert response.status_code == 202
    assert isinstance(submitted[0].source, FileSource)


async def test_multipart_string_preserves_original_content(monkeypatch) -> None:
    submitted = []

    async def fake_submit_add(service, payload):
        submitted.append(payload)
        return TaskAccepted(task_id="b" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_add", fake_submit_add)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            files={
                "user_id": (None, "user-001"),
                "workspace_id": (None, WORKSPACE_ID),
                "workspace_name": (None, "产品知识库"),
                "type": (None, "str"),
                "content": (None, "  Exact\ntext  "),
            },
        )

    assert response.status_code == 202
    payload = submitted[0]
    assert isinstance(payload.source, StringSource)
    assert payload.source.content == "  Exact\ntext  "


async def test_multipart_accepts_optional_callback_url(monkeypatch) -> None:
    submitted = []

    async def fake_submit_add(service, payload):
        submitted.append(payload)
        return TaskAccepted(task_id="e" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_add", fake_submit_add)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/add",
            files={
                "user_id": (None, "user-001"),
                "workspace_id": (None, WORKSPACE_ID),
                "workspace_name": (None, "产品知识库"),
                "type": (None, "str"),
                "content": (None, "value"),
                "callback_url": (None, "https://example.com/callback"),
            },
        )
    assert response.status_code == 202
    assert str(submitted[0].callback_url) == "https://example.com/callback"


async def test_delete_forwards_optional_callback_url(monkeypatch) -> None:
    delete_submissions = []

    async def fake_submit_delete(service, payload):
        delete_submissions.append(payload)
        return TaskAccepted(task_id="b" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_delete", fake_submit_delete)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    callback_url = "https://example.com/task-callback"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        deletion = await client.post(
            "/api/v1/delete",
            json={
                "user_id": "user-001",
                "workspace_id": WORKSPACE_ID,
                "file_id": "file_550e8400-e29b-41d4-a716-446655440000",
                "file_name": "report.pdf",
                "callback_url": callback_url,
            },
        )

    assert deletion.status_code == 202
    assert str(delete_submissions[0].callback_url) == callback_url


async def test_delete_string_uses_stable_content_hash(monkeypatch) -> None:
    submissions = []

    async def fake_submit_delete_string(service, payload):
        submissions.append(payload)
        return TaskAccepted(task_id="f" * 32)

    monkeypatch.setattr(api_main.FileService, "submit_delete_string", fake_submit_delete_string)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    digest = "sha256:" + "a" * 32
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/delete-string",
            json={"user_id": "user-001", "workspace_id": WORKSPACE_ID, "content_hash": digest},
        )
        invalid = await client.post(
            "/api/v1/delete-string",
            json={"user_id": "user-001", "workspace_id": WORKSPACE_ID, "content_hash": "not-a-hash"},
        )

    assert response.status_code == 202
    assert invalid.status_code == 422
    assert isinstance(submissions[0], DeleteStringRequest)
    assert submissions[0].content_hash == digest


async def test_file_list_returns_result_directly(monkeypatch) -> None:
    current_workspace_id = WORKSPACE_ID

    async def fake_list_files(service, requested_user_id, requested_workspace_id, *, include_string_content=False):
        assert requested_user_id == "user-001"
        assert requested_workspace_id == current_workspace_id
        assert include_string_content is False
        return FileListResponse(
            workspace_id=current_workspace_id,
            files=[],
            strings=[
                StringListItem(
                    content_hash="sha256:" + "a" * 32,
                    size_bytes=6,
                    created_at="2026-08-04T06:00:00Z",
                    modified_at="2026-08-04T06:00:00Z",
                )
            ],
            stats=WorkspaceStats(resource_count=1, str_count=1, total_size_bytes=6),
        )

    monkeypatch.setattr(api_main.FileService, "list_files", fake_list_files)
    app = create_app(FakeRuntime())  # type: ignore[arg-type]
    app.state.runtime = FakeRuntime()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_user = await client.get(f"/api/v1/workspaces/{current_workspace_id}/files")
        response = await client.get(
            f"/api/v1/workspaces/{current_workspace_id}/files",
            params={"user_id": "user-001"},
        )

    assert missing_user.status_code == 422
    assert response.status_code == 200
    assert response.json()["stats"]["resource_count"] == 1
    assert "content" not in response.json()["strings"][0]
