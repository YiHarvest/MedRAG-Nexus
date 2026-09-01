"""装配应用路由、领域存储与后台维护任务。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from medrag_nexus.agent import AgentContext, AgentStore, ArtifactService, build_agent_tool_registry
from medrag_nexus.agent.router import create_agent_router
from medrag_nexus.agent.service import AgentCapabilityService
from medrag_nexus.core.config import Settings
from medrag_nexus.core.paths import API_V1_PREFIX, HEALTH_API_PREFIX
from medrag_nexus.identity.audit import AuditLogExporter, reset_audit_request_id, set_audit_request_id
from medrag_nexus.identity.models import RegisterAccountRequest
from medrag_nexus.identity.permissions import build_default_registry
from medrag_nexus.identity.router import DEFAULT_COOKIE_NAME, create_account_router
from medrag_nexus.identity.security import WEBUI_LOCK_COOKIE_NAME, PasswordService, verify_webui_lock_session
from medrag_nexus.identity.store import AccountConflictError, AccountStore
from medrag_nexus.knowledge.policies import KnowledgePolicyStore
from medrag_nexus.knowledge.router import create_knowledge_router
from medrag_nexus.services.runtime import Runtime


def _is_protected_api_path(path: str) -> bool:
    """Return whether a path belongs to the authenticated v1 business API."""

    return path.startswith(f"{API_V1_PREFIX}/") and not path.startswith(f"{HEALTH_API_PREFIX}/")


class ApplicationLifecycle:
    """集中管理应用存储、路由装配、启动和清理。"""

    def __init__(self, runtime: Runtime, settings: Settings):
        self.runtime = runtime
        self.settings = settings
        self.registry = build_default_registry()
        runtime_settings = getattr(runtime, "settings", None)
        metadata = getattr(runtime, "metadata", None)
        database_path = getattr(runtime_settings, "sqlite_path", None) or getattr(
            metadata,
            "path",
            settings.sqlite_path,
        )
        self.store = AccountStore(database_path, self.registry)
        self.policies = KnowledgePolicyStore(database_path)
        self.agent_store = AgentStore(database_path)
        data_root = getattr(runtime_settings, "data_root", None) or getattr(
            settings,
            "webui_data_root",
            database_path.parent,
        )
        self.agent_artifacts = ArtifactService(data_root / "webui_agent_artifacts", self.agent_store)
        self.agent_registry = build_agent_tool_registry()
        self.agent_capabilities = AgentCapabilityService(
            action_store=self.agent_store,
            artifacts=self.agent_artifacts,
            action_ttl=timedelta(minutes=getattr(settings, "webui_agent_action_ttl_minutes", 15)),
            artifact_ttl=timedelta(hours=getattr(settings, "webui_agent_artifact_ttl_hours", 24)),
        )
        self.audit_exporter = AuditLogExporter(
            database_path,
            getattr(settings, "webui_audit_log_dir", database_path.parent / "audit" / "webui"),
            getattr(settings, "webui_audit_log_retention_months", 3),
        )
        self._cleanup_task: asyncio.Task[None] | None = None
        self._audit_export_task: asyncio.Task[None] | None = None
        self._agent_cleanup_task: asyncio.Task[None] | None = None

    def install(self, app: FastAPI) -> None:
        def create_chat_agent_context(**values: Any) -> AgentContext:
            return AgentContext(
                **values,
                action_store=self.agent_store,
                capability_gateway=self.agent_capabilities,
            )

        app.include_router(
            create_account_router(
                self.store,
                self.registry,
                cookie_secure=self.settings.webui_cookie_secure,
            )
        )
        app.include_router(
            create_knowledge_router(
                self.runtime,
                self.store,
                self.policies,
                agent_context_factory=create_chat_agent_context,
                agent_registry=self.agent_registry,
            )
        )
        app.include_router(
            create_agent_router(
                self.runtime,
                self.store,
                self.policies,
                self.agent_store,
                self.agent_artifacts,
            )
        )

        async def audit_backend_requests(request: Request, call_next):
            """记录全部后端业务请求；密码、正文和 Cookie 永不进入日志。"""

            if not _is_protected_api_path(request.url.path):
                return await call_next(request)
            supplied_request_id = request.headers.get("x-request-id", "").strip()
            request_id = supplied_request_id[:128] if supplied_request_id else uuid4().hex
            request_context = set_audit_request_id(request_id)
            actor = None
            token = request.cookies.get(DEFAULT_COOKIE_NAME)
            if token:
                try:
                    actor = await self.store.authenticate_session(token)
                except Exception:
                    logging.getLogger(__name__).warning("读取后端审计身份失败", exc_info=True)
            started = time.perf_counter()
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            finally:
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                method = request.method.upper()
                operation = (
                    "view"
                    if method in {"GET", "HEAD"}
                    else "delete"
                    if method == "DELETE"
                    else "modify"
                    if method in {"PATCH", "PUT"}
                    else "execute"
                )
                route = request.scope.get("route")
                route_path = getattr(route, "path", request.url.path)
                detail = {
                    "method": method,
                    "path": request.url.path,
                    "route": route_path,
                    "query_keys": sorted(request.query_params.keys()),
                    "status_code": status_code,
                    "succeeded": status_code < 400,
                    "duration_ms": duration_ms,
                    "client_ip": self._client_ip(request),
                    "user_agent": request.headers.get("user-agent", "")[:512],
                }
                try:
                    await self.store.record_audit(
                        actor_account_id=actor.account_id if actor is not None else None,
                        action=f"webui.http.{operation}",
                        resource_type="http_request",
                        resource_id=f"{method} {route_path}",
                        after=detail,
                        request_id=request_id,
                    )
                except Exception:
                    logging.getLogger(__name__).error(
                        "后端请求审计写入失败",
                        extra={"request_id": request_id, "path": request.url.path},
                        exc_info=True,
                    )
                logging.getLogger("uvicorn.error").info(
                    "[后端审计] account_id=%s login_name=%s method=%s path=%s status=%s "
                    "duration_ms=%s client_ip=%s request_id=%s",
                    actor.account_id if actor is not None else "anonymous",
                    actor.login_name if actor is not None else "anonymous",
                    method,
                    request.url.path,
                    status_code,
                    duration_ms,
                    detail["client_ip"],
                    request_id,
                )
                reset_audit_request_id(request_context)

        @app.middleware("http")
        async def protect_backend_cookie_mutations(request: Request, call_next):
            """验证外层门锁，并拒绝跨站修改后端数据。"""

            is_backend_api = _is_protected_api_path(request.url.path)
            lock_password = self.settings.webui_lock_password.strip()
            if (
                is_backend_api
                and lock_password
                and not verify_webui_lock_session(
                    request.cookies.get(WEBUI_LOCK_COOKIE_NAME),
                    lock_password,
                )
            ):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": {
                            "code": "outer_lock_required",
                            "message": "WebUI outer lock session is required",
                        }
                    },
                    headers={"cache-control": "no-store"},
                )
            if is_backend_api and request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "origin_denied",
                                "message": "cross-site mutation is not allowed",
                            }
                        },
                    )
                origin = request.headers.get("origin")
                forwarded_host = request.headers.get("x-forwarded-host")
                if origin and forwarded_host:
                    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                    if origin.rstrip("/") != f"{scheme}://{forwarded_host}".rstrip("/"):
                        return JSONResponse(
                            status_code=403,
                            content={
                                "detail": {
                                    "code": "origin_denied",
                                    "message": "cross-origin mutation is not allowed",
                                }
                            },
                        )
            return await call_next(request)

        # 最后注册审计中间件，使它位于安全校验之外；被门锁、来源校验或权限
        # 校验拒绝的尝试同样需要留下可追溯记录。
        app.middleware("http")(audit_backend_requests)

    async def start(self) -> None:
        await self.store.ensure()
        await self.policies.ensure()
        await self.agent_store.ensure()
        await self.audit_exporter.ensure()
        await self.audit_exporter.export_pending()
        await self._retry_failed_workspace_deletions()
        if not await self.store.list_accounts():
            await self._bootstrap_superadmins()
        await self._ensure_existing_resource_acls()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_failed_deletions_loop(),
                name="webui-workspace-cleanup",
            )
        if self._audit_export_task is None or self._audit_export_task.done():
            self._audit_export_task = asyncio.create_task(
                self._audit_export_loop(),
                name="webui-audit-export",
            )
        if self._agent_cleanup_task is None or self._agent_cleanup_task.done():
            self._agent_cleanup_task = asyncio.create_task(
                self._agent_cleanup_loop(),
                name="webui-agent-cleanup",
            )

    async def _bootstrap_superadmins(self) -> None:
        """空库时从 JSON 列表或兼容的单账号配置创建超级管理员。"""

        raw = str(getattr(self.settings, "webui_superadmins_json", "") or "").strip()
        configured: list[RegisterAccountRequest] = []
        if raw:
            try:
                values = json.loads(raw)
                if not isinstance(values, list) or not values:
                    raise ValueError("WEBUI_SUPERADMINS_JSON must be a non-empty JSON array")
                for item in values:
                    if not isinstance(item, dict):
                        raise ValueError("each superadmin must be a JSON object")
                    login_name = item.get("login_name", item.get("username"))
                    configured.append(
                        RegisterAccountRequest(
                            login_name=login_name,
                            display_name=item.get("display_name", login_name),
                            password=item.get("password"),
                        )
                    )
            except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                raise RuntimeError("WEBUI_SUPERADMINS_JSON configuration is invalid") from exc
        else:
            username = str(getattr(self.settings, "webui_superadmin_username", "") or "").strip()
            password = str(getattr(self.settings, "webui_superadmin_password", "") or "")
            if username and password:
                try:
                    configured.append(
                        RegisterAccountRequest(
                            login_name=username,
                            display_name=str(getattr(self.settings, "webui_superadmin_display_name", "超级管理员")),
                            password=password,
                        )
                    )
                except ValidationError as exc:
                    raise RuntimeError("WEBUI_SUPERADMIN_* configuration is invalid") from exc
        if not configured:
            return
        if len({item.login_name.casefold() for item in configured}) != len(configured):
            raise RuntimeError("WEBUI superadmin login names must be unique")
        try:
            first = configured[0]
            first_account = await self.store.bootstrap_superadmin(
                login_name=first.login_name,
                display_name=first.display_name,
                password_hash=await asyncio.to_thread(PasswordService().hash, first.password),
            )
            for item in configured[1:]:
                await self.store.create_account(
                    login_name=item.login_name,
                    display_name=item.display_name,
                    password_hash=await asyncio.to_thread(PasswordService().hash, item.password),
                    permission_level=1000,
                    group_keys=[],
                    bound_user_id=None,
                    must_change_password=False,
                    actor_account_id=first_account.account_id,
                    audit_action="webui.account.bootstrap_superadmin",
                )
        except AccountConflictError:
            # 多进程启动时，另一个进程可能已经先完成初始化。
            return

    async def _ensure_existing_resource_acls(self) -> None:
        """为后端既有资源补齐超级管理员和负责人的系统 ACL。"""

        metadata = getattr(self.runtime, "metadata", None)
        if metadata is None:
            return
        for user in (await metadata.list_users()).users:
            await self.policies.ensure_resource_acl("user", user.user_id)
            for workspace in (await metadata.list_workspaces(user.user_id)).workspaces:
                await self.policies.ensure_resource_acl(
                    "workspace",
                    workspace.workspace_id,
                    user_id=user.user_id,
                )

    async def close(self) -> None:
        """停止后端维护任务。"""

        for task in (self._cleanup_task, self._audit_export_task, self._agent_cleanup_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._cleanup_task = None
        self._audit_export_task = None
        self._agent_cleanup_task = None
        await self.audit_exporter.export_pending()

    async def _audit_export_loop(self) -> None:
        """持续导出已提交事件，并每天执行一次过期日志清理。"""

        last_cleanup = time.monotonic()
        while True:
            await asyncio.sleep(1)
            try:
                while await self.audit_exporter.export_pending():
                    pass
                if time.monotonic() - last_cleanup >= 86400:
                    await self.audit_exporter.cleanup()
                    last_cleanup = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).error("WebUI 审计日志导出失败，将自动重试", exc_info=True)

    def _client_ip(self, request: Request) -> str | None:
        """仅在显式信任代理时解析转发地址，避免客户端伪造审计来源。"""

        direct = request.client.host if request.client is not None else None
        if not getattr(self.settings, "webui_trust_proxy_headers", False):
            return direct
        forwarded = [value.strip() for value in request.headers.get("x-forwarded-for", "").split(",") if value.strip()]
        if not forwarded:
            return direct
        index = max(0, len(forwarded) - getattr(self.settings, "webui_trusted_proxy_hops", 1))
        return forwarded[index]

    async def _cleanup_failed_deletions_loop(self) -> None:
        """持续重试已经隐藏但尚未清理完成的 Workspace。"""

        while True:
            await asyncio.sleep(self.settings.webui_cleanup_retry_seconds)
            await self._retry_failed_workspace_deletions()

    async def _agent_cleanup_loop(self) -> None:
        """定期清理过期确认单、终态元数据和临时制品。"""

        interval = getattr(self.settings, "webui_agent_cleanup_interval_seconds", 3600)
        retention = timedelta(days=getattr(self.settings, "webui_agent_action_retention_days", 30))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.agent_store.expire_due_actions()
                await self.agent_store.cleanup_completed_actions(retention=retention)
                await self.agent_artifacts.cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).error("WebUI Agent 临时数据清理失败，将自动重试", exc_info=True)

    async def _retry_failed_workspace_deletions(self) -> None:
        lease_seconds = getattr(self.settings, "webui_deletion_lease_seconds", 300)
        pending = await self.policies.pending_deletions(deleting_lease_seconds=lease_seconds)
        for deletion in pending:
            workspace_id = deletion.workspace_id
            operation_id, owner_user_id = self._deletion_context(workspace_id, deletion.detail)
            try:
                metadata = getattr(self.runtime, "metadata", None)
                workspace = await metadata.get_workspace(workspace_id) if metadata is not None else None
                if workspace is not None:
                    owner_user_id = workspace.user_id
                async with self._workspace_cleanup_lock(owner_user_id, workspace_id):
                    current_state = await self.policies.lifecycle(workspace_id)
                    if current_state not in {"deleting", "delete_failed"}:
                        continue
                    workspace = await metadata.get_workspace(workspace_id) if metadata is not None else None
                    if workspace is not None:
                        if await metadata.workspace_has_active_tasks(workspace_id):
                            continue
                        await self.runtime.artifacts.move_workspace_to_recycle(
                            operation_id,
                            workspace.user_id,
                            workspace_id,
                        )
                        await metadata.delete_workspace(workspace_id)
                    await self.policies.delete_workspace_policy_data(workspace_id)
                    await self.runtime.elasticsearch.delete_workspace_contents(workspace_id)
                    await self.runtime.milvus.delete_workspace(workspace_id)
                    documents, chunks = await self.runtime.elasticsearch.count_workspace_contents(workspace_id)
                    vectors = await self.runtime.milvus.count_workspace(workspace_id)
                    if documents or chunks or vectors:
                        raise RuntimeError("workspace index cleanup verification failed")
                    await self.runtime.artifacts.cleanup_recycle(operation_id)
                    await self.policies.mark_lifecycle(
                        workspace_id,
                        "deleted",
                        actor_account_id="system",
                    )
                await self.store.record_audit(
                    actor_account_id=None,
                    action="webui.workspace.delete.recovered",
                    resource_type="workspace",
                    resource_id=workspace_id,
                    after={"status": "deleted", "operation_id": operation_id},
                )
            except Exception as exc:
                # SQLite 权威元数据已隐藏；保留墓碑并等待下一轮后台重试。
                await self.store.record_audit(
                    actor_account_id=None,
                    action="webui.workspace.delete.retry_failed",
                    resource_type="workspace",
                    resource_id=workspace_id,
                    after={
                        "status": "cleanup_pending",
                        "operation_id": operation_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

    @staticmethod
    def _deletion_context(workspace_id: str, detail: str | None) -> tuple[str, str | None]:
        """兼容读取新版 JSON 与旧版竖线分隔的删除上下文。"""

        if detail:
            try:
                value = json.loads(detail)
                if isinstance(value, dict):
                    operation_id = value.get("operation_id")
                    user_id = value.get("user_id")
                    if isinstance(operation_id, str) and operation_id.startswith("workspace-delete-"):
                        return operation_id, user_id if isinstance(user_id, str) else None
            except json.JSONDecodeError:
                pass
            legacy_operation_id = detail.partition("|")[0]
            if legacy_operation_id.startswith("workspace-delete-"):
                return legacy_operation_id, None
        return f"workspace-delete-recovery-{workspace_id}", None

    @asynccontextmanager
    async def _workspace_cleanup_lock(self, user_id: str | None, workspace_id: str) -> AsyncIterator[None]:
        """恢复删除时复用业务 Workspace 分布式锁；旧墓碑保持向后兼容。"""

        tasks = getattr(self.runtime, "tasks", None)
        if tasks is None:
            yield
            return
        lock_owner = user_id or "__webui_cleanup__"
        async with tasks.workspace_lock(lock_owner, workspace_id):
            yield
