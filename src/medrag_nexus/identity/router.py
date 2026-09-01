"""后端账号注册、Session、权限与审计 API。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status

from medrag_nexus.core.paths import API_V1_PREFIX

from .models import (
    AccountCapabilities,
    AccountListResponse,
    AccountRecord,
    AccountResponse,
    AdminCreateAccountRequest,
    AdminPatchAccountRequest,
    AdminResetPasswordRequest,
    AuditEventListResponse,
    ChangePasswordRequest,
    CreatePermissionGroupRequest,
    LoginRequest,
    MessageResponse,
    PatchPermissionGroupRequest,
    PermissionCatalogResponse,
    PermissionGroupListResponse,
    PermissionGroupResponse,
    RegisterAccountRequest,
    SessionResponse,
)
from .permissions import PermissionEngine, PermissionRegistry
from .security import PasswordService
from .store import (
    AccountConflictError,
    AccountNotFoundError,
    AccountStore,
    InvalidPermissionGroupError,
    InvalidPermissionLevelError,
    LastSuperadminError,
    PermissionGroupConflictError,
    PermissionGroupNotFoundError,
    SuperadminImmutableError,
)

DEFAULT_COOKIE_NAME = "medrag_nexus_webui_account_session"


@dataclass(frozen=True, slots=True)
class AccountPrincipal:
    """由服务端 Session 推导的调用身份，请求体不能指定该值。"""

    account: AccountRecord
    permissions: frozenset[str]

    @property
    def account_id(self) -> str:
        return self.account.account_id

    @property
    def bound_user_id(self) -> str | None:
        return self.account.bound_user_id


PrincipalDependency = Callable[..., Awaitable[AccountPrincipal]]


def create_principal_dependency(store: AccountStore, *, cookie_name: str = DEFAULT_COOKIE_NAME) -> PrincipalDependency:
    async def current_principal(
        session_token: str | None = Cookie(default=None, alias=cookie_name),
    ) -> AccountPrincipal:
        if not session_token:
            raise _http_error(status.HTTP_401_UNAUTHORIZED, "authentication_required", "account login is required")
        account = await store.authenticate_session(session_token)
        if account is None:
            raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_session", "session is invalid or expired")
        permissions = await store.permission_keys(account.account_id)
        return AccountPrincipal(account=account, permissions=frozenset(permissions))

    return current_principal


def require_permission(
    principal_dependency: PrincipalDependency,
    permission: str,
    engine: PermissionEngine,
) -> PrincipalDependency:
    async def authorized(principal: Annotated[AccountPrincipal, Depends(principal_dependency)]) -> AccountPrincipal:
        if principal.account.must_change_password:
            raise _http_error(
                status.HTTP_403_FORBIDDEN,
                "password_change_required",
                "password must be changed before using this operation",
            )
        if not engine.allows(permission, principal.permissions):
            raise _http_error(status.HTTP_403_FORBIDDEN, "permission_denied", f"missing permission: {permission}")
        return principal

    return authorized


def create_account_router(
    store: AccountStore,
    registry: PermissionRegistry | None = None,
    *,
    cookie_name: str = DEFAULT_COOKIE_NAME,
    cookie_secure: bool = False,
    session_ttl: timedelta = timedelta(hours=12),
) -> APIRouter:
    """构建可通过 ``app.include_router(router)`` 挂载的后端账号路由。

    正常情况下应在应用生命周期中调用 ``await store.ensure()``。存储方法也会
    防御性地执行初始化，避免测试或小型嵌入场景误查尚未创建的表。
    """

    selected_registry = registry or store.registry
    engine = PermissionEngine(selected_registry)
    passwords = PasswordService()
    principal_dependency = create_principal_dependency(store, cookie_name=cookie_name)
    account_manage = require_permission(principal_dependency, "webui.account.manage", engine)
    account_create = require_permission(principal_dependency, "webui.account.create", engine)
    password_reset = require_permission(principal_dependency, "webui.account.password.reset", engine)
    audit_read = require_permission(principal_dependency, "webui.audit.read", engine)
    catalog_read = require_permission(principal_dependency, "webui.permission.catalog.read", engine)
    group_manage = require_permission(principal_dependency, "webui.permission.group.manage", engine)

    router = APIRouter(prefix=API_V1_PREFIX, tags=["认证与账号"])

    @router.post("/auth/register", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
    async def register(payload: RegisterAccountRequest, response: Response) -> SessionResponse:
        encoded = await asyncio.to_thread(passwords.hash, payload.password)
        try:
            account = await store.register_account(
                login_name=payload.login_name,
                display_name=payload.display_name,
                password_hash=encoded,
            )
        except AccountConflictError as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        token, expires_at = await store.create_session(account, session_ttl)
        await store.record_audit(
            actor_account_id=account.account_id,
            action="webui.session.create",
            resource_type="session",
            resource_id=account.account_id,
            after={"source": "register"},
        )
        permissions = sorted(await store.permission_keys(account.account_id))
        _set_session_cookie(
            response,
            token,
            cookie_name=cookie_name,
            cookie_secure=cookie_secure,
            max_age=int(session_ttl.total_seconds()),
        )
        return SessionResponse(
            account=AccountResponse.from_record(account), permissions=permissions, expires_at=expires_at
        )

    @router.post("/auth/login", response_model=SessionResponse)
    async def login(payload: LoginRequest, response: Response) -> SessionResponse:
        account = await store.get_account_by_login(payload.login_name)
        if (
            account is not None
            and account.locked_until is not None
            and account.locked_until > datetime.now(timezone.utc)
        ):
            await store.record_audit(
                actor_account_id=account.account_id,
                action="webui.session.login_denied",
                resource_type="account",
                resource_id=account.account_id,
                after={"reason": "account_locked", "login_name": account.login_name},
            )
            raise _http_error(status.HTTP_423_LOCKED, "account_locked", "account is temporarily locked")
        if account is None or not await asyncio.to_thread(passwords.verify, account.password_hash, payload.password):
            if account is not None:
                await store.record_login_failure(account.account_id)
            await store.record_audit(
                actor_account_id=account.account_id if account is not None else None,
                action="webui.session.login_failed",
                resource_type="account" if account is not None else "login_name",
                resource_id=account.account_id if account is not None else payload.login_name,
                after={"reason": "invalid_credentials", "login_name": payload.login_name},
            )
            raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "invalid login name or password")
        if not account.enabled:
            await store.record_audit(
                actor_account_id=account.account_id,
                action="webui.session.login_denied",
                resource_type="account",
                resource_id=account.account_id,
                after={"reason": "account_disabled", "login_name": account.login_name},
            )
            raise _http_error(status.HTTP_403_FORBIDDEN, "account_disabled", "account is disabled")
        if passwords.needs_rehash(account.password_hash):
            encoded = await asyncio.to_thread(passwords.hash, payload.password)
            await store.replace_password(
                account_id=account.account_id,
                password_hash=encoded,
                must_change_password=account.must_change_password,
                actor_account_id=account.account_id,
                audit_action="webui.account.password.rehash",
            )
        account = await store.record_login_success(account.account_id)
        token, expires_at = await store.create_session(account, session_ttl)
        await store.record_audit(
            actor_account_id=account.account_id,
            action="webui.session.create",
            resource_type="session",
            resource_id=account.account_id,
            after={"source": "login"},
        )
        permissions = sorted(await store.permission_keys(account.account_id))
        _set_session_cookie(
            response,
            token,
            cookie_name=cookie_name,
            cookie_secure=cookie_secure,
            max_age=int(session_ttl.total_seconds()),
        )
        return SessionResponse(
            account=AccountResponse.from_record(account), permissions=permissions, expires_at=expires_at
        )

    @router.post("/auth/logout", response_model=MessageResponse)
    async def logout(
        response: Response,
        session_token: str | None = Cookie(default=None, alias=cookie_name),
    ) -> MessageResponse:
        if session_token:
            account = await store.authenticate_session(session_token)
            await store.revoke_session(session_token)
            if account is not None:
                await store.record_audit(
                    actor_account_id=account.account_id,
                    action="webui.session.revoke_self",
                    resource_type="session",
                    resource_id=account.account_id,
                )
        response.delete_cookie(cookie_name, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return MessageResponse()

    @router.get("/auth/me", response_model=SessionResponse)
    async def me(principal: Annotated[AccountPrincipal, Depends(principal_dependency)]) -> SessionResponse:
        return SessionResponse(
            account=AccountResponse.from_record(principal.account), permissions=sorted(principal.permissions)
        )

    @router.post("/account/password", response_model=MessageResponse)
    async def change_password(
        payload: ChangePasswordRequest,
        principal: Annotated[AccountPrincipal, Depends(principal_dependency)],
    ) -> MessageResponse:
        if not engine.allows("webui.account.password.change_self", principal.permissions):
            raise _http_error(status.HTTP_403_FORBIDDEN, "permission_denied", "password change is not allowed")
        if not await asyncio.to_thread(passwords.verify, principal.account.password_hash, payload.current_password):
            raise _http_error(status.HTTP_400_BAD_REQUEST, "current_password_invalid", "current password is incorrect")
        if payload.new_password.casefold() == principal.account.login_name.casefold():
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "weak_password", "password must not equal login name"
            )
        if await asyncio.to_thread(passwords.verify, principal.account.password_hash, payload.new_password):
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "password_unchanged", "new password must be different"
            )
        encoded = await asyncio.to_thread(passwords.hash, payload.new_password)
        await store.replace_password(
            account_id=principal.account_id,
            password_hash=encoded,
            must_change_password=False,
            actor_account_id=principal.account_id,
            audit_action="webui.account.password.change_self",
        )
        return MessageResponse()

    @router.get("/accounts", response_model=AccountListResponse)
    async def list_accounts(
        principal: Annotated[AccountPrincipal, Depends(account_manage)],
    ) -> AccountListResponse:
        accounts = await store.list_accounts()
        permission_sets = await asyncio.gather(*(store.permission_keys(account.account_id) for account in accounts))
        items = [
            _account_response(account, principal, permissions)
            for account, permissions in zip(accounts, permission_sets, strict=True)
        ]
        return AccountListResponse(accounts=items, total=len(items))

    @router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
    async def create_account(
        payload: AdminCreateAccountRequest,
        principal: Annotated[AccountPrincipal, Depends(account_create)],
    ) -> AccountResponse:
        _require_superadmin(principal)
        _validate_admin_assignment(
            actor=principal,
            requested_level=payload.permission_level,
            requested_groups=payload.group_keys,
            engine=engine,
        )
        encoded = await asyncio.to_thread(passwords.hash, payload.password)
        try:
            account = await store.create_account(
                login_name=payload.login_name,
                display_name=payload.display_name,
                password_hash=encoded,
                permission_level=payload.permission_level,
                group_keys=payload.group_keys,
                bound_user_id=None,
                must_change_password=payload.must_change_password,
                actor_account_id=principal.account_id,
            )
        except (AccountConflictError, InvalidPermissionGroupError, InvalidPermissionLevelError) as exc:
            code = (
                status.HTTP_409_CONFLICT
                if isinstance(exc, AccountConflictError)
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise _store_http_error(exc, code) from exc
        return _account_response(account, principal, await store.permission_keys(account.account_id))

    @router.patch("/accounts/{account_id}", response_model=AccountResponse)
    async def patch_account(
        account_id: str,
        payload: AdminPatchAccountRequest,
        principal: Annotated[AccountPrincipal, Depends(account_manage)],
    ) -> AccountResponse:
        target = await store.get_account(account_id)
        if target is None:
            raise _http_error(status.HTTP_404_NOT_FOUND, "account_not_found", "account does not exist")
        _reject_superadmin_target(target)
        _validate_target_level(principal, target)
        requested_level = target.permission_level if payload.permission_level is None else payload.permission_level
        requested_groups = target.groups if payload.group_keys is None else payload.group_keys
        _validate_admin_assignment(
            actor=principal,
            requested_level=requested_level,
            requested_groups=requested_groups,
            engine=engine,
        )
        try:
            account = await store.patch_account(
                account_id=account_id,
                patch=payload,
                actor_account_id=principal.account_id,
            )
        except AccountNotFoundError as exc:
            raise _store_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
        except InvalidPermissionGroupError as exc:
            raise _store_http_error(exc, status.HTTP_422_UNPROCESSABLE_CONTENT) from exc
        except (LastSuperadminError, SuperadminImmutableError) as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        return _account_response(account, principal, await store.permission_keys(account.account_id))

    @router.post("/accounts/{account_id}/password/reset", response_model=MessageResponse)
    async def reset_account_password(
        account_id: str,
        payload: AdminResetPasswordRequest,
        principal: Annotated[AccountPrincipal, Depends(password_reset)],
    ) -> MessageResponse:
        target = await store.get_account(account_id)
        if target is None:
            raise _http_error(status.HTTP_404_NOT_FOUND, "account_not_found", "account does not exist")
        _reject_superadmin_target(target)
        _validate_target_level(principal, target)
        if payload.new_password.casefold() == target.login_name.casefold():
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "weak_password", "password must not equal login name"
            )
        encoded = await asyncio.to_thread(passwords.hash, payload.new_password)
        try:
            await store.replace_password(
                account_id=account_id,
                password_hash=encoded,
                must_change_password=payload.must_change_password,
                actor_account_id=principal.account_id,
                audit_action="webui.account.password.reset",
            )
        except SuperadminImmutableError as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        return MessageResponse()

    @router.get("/permission-catalog", response_model=PermissionCatalogResponse)
    async def permission_catalog(
        _principal: Annotated[AccountPrincipal, Depends(catalog_read)],
    ) -> PermissionCatalogResponse:
        return await store.permission_catalog()

    @router.get("/permission-groups", response_model=PermissionGroupListResponse)
    async def list_permission_groups(
        _principal: Annotated[AccountPrincipal, Depends(catalog_read)],
    ) -> PermissionGroupListResponse:
        groups = await store.list_permission_groups()
        return PermissionGroupListResponse(groups=groups, total=len(groups))

    @router.post("/permission-groups", response_model=PermissionGroupResponse, status_code=status.HTTP_201_CREATED)
    async def create_permission_group(
        payload: CreatePermissionGroupRequest,
        principal: Annotated[AccountPrincipal, Depends(group_manage)],
    ) -> PermissionGroupResponse:
        _require_superadmin(principal)
        try:
            return await store.create_permission_group(
                group_key=payload.group_key,
                name=payload.name,
                description=payload.description,
                permissions=payload.permissions,
                actor_account_id=principal.account_id,
            )
        except PermissionGroupConflictError as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        except InvalidPermissionGroupError as exc:
            raise _store_http_error(exc, status.HTTP_422_UNPROCESSABLE_CONTENT) from exc

    @router.patch("/permission-groups/{group_key}", response_model=PermissionGroupResponse)
    async def patch_permission_group(
        group_key: str,
        payload: PatchPermissionGroupRequest,
        principal: Annotated[AccountPrincipal, Depends(group_manage)],
    ) -> PermissionGroupResponse:
        _require_superadmin(principal)
        try:
            return await store.patch_permission_group(
                group_key=group_key, patch=payload, actor_account_id=principal.account_id
            )
        except PermissionGroupNotFoundError as exc:
            raise _store_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
        except PermissionGroupConflictError as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        except InvalidPermissionGroupError as exc:
            raise _store_http_error(exc, status.HTTP_422_UNPROCESSABLE_CONTENT) from exc

    @router.delete("/permission-groups/{group_key}", response_model=MessageResponse)
    async def delete_permission_group(
        group_key: str,
        principal: Annotated[AccountPrincipal, Depends(group_manage)],
    ) -> MessageResponse:
        _require_superadmin(principal)
        try:
            await store.delete_permission_group(group_key=group_key, actor_account_id=principal.account_id)
        except PermissionGroupNotFoundError as exc:
            raise _store_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
        except PermissionGroupConflictError as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        return MessageResponse()

    @router.delete("/account/permission-groups/{group_key}", response_model=AccountResponse)
    async def leave_permission_group(
        group_key: str,
        principal: Annotated[AccountPrincipal, Depends(principal_dependency)],
    ) -> AccountResponse:
        """当前账号只能退出自己的自定义权限组。"""

        try:
            account = await store.leave_permission_group(
                account_id=principal.account_id,
                group_key=group_key,
            )
        except PermissionGroupNotFoundError as exc:
            raise _store_http_error(exc, status.HTTP_404_NOT_FOUND) from exc
        except (PermissionGroupConflictError, SuperadminImmutableError) as exc:
            raise _store_http_error(exc, status.HTTP_409_CONFLICT) from exc
        return _account_response(account, principal, await store.permission_keys(account.account_id))

    @router.get("/audit-events", response_model=AuditEventListResponse)
    async def list_audit_events(
        _principal: Annotated[AccountPrincipal, Depends(audit_read)],
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> AuditEventListResponse:
        events, total = await store.list_audit_events(limit=limit, offset=offset)
        return AuditEventListResponse(events=events, total=total)

    return router


def _validate_admin_assignment(
    *,
    actor: AccountPrincipal,
    requested_level: int,
    requested_groups: list[str],
    engine: PermissionEngine,
) -> None:
    _require_superadmin(actor)
    if engine.registry.level(requested_level) is None:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_permission_level",
            "permission level is not registered by an available plugin",
        )
    if requested_level > actor.account.permission_level:
        raise _http_error(status.HTTP_403_FORBIDDEN, "level_escalation_denied", "cannot assign a level above your own")
    is_superadmin = requested_level == 1000
    if is_superadmin and requested_groups:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_superadmin_assignment",
            "superadmin accounts use level permissions and cannot join organizational groups",
        )
    if is_superadmin and not engine.allows("webui.account.create_superadmin", actor.permissions):
        raise _http_error(status.HTTP_403_FORBIDDEN, "permission_denied", "cannot create or promote a superadmin")


def _require_superadmin(principal: AccountPrincipal) -> None:
    if principal.account.permission_level != 1000:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "superadmin_required",
            "this operation requires an immutable superadmin account",
        )


def _reject_superadmin_target(target: AccountRecord) -> None:
    if target.permission_level == 1000:
        raise _http_error(
            status.HTTP_409_CONFLICT,
            "superadmin_immutable",
            "superadmin accounts cannot be modified, reset, or disabled",
        )


def _account_response(
    account: AccountRecord,
    actor: AccountPrincipal,
    permissions: set[str] | frozenset[str] = frozenset(),
) -> AccountResponse:
    protected = account.permission_level == 1000
    actor_is_superadmin = actor.account.permission_level == 1000
    response = AccountResponse.from_record(account)
    response.permissions = sorted(permissions)
    response.capabilities = AccountCapabilities(
        can_update=actor_is_superadmin and not protected,
        can_reset_password=actor_is_superadmin and not protected,
        can_bind_user=actor_is_superadmin and not protected,
        protected=protected,
    )
    return response


def _validate_target_level(actor: AccountPrincipal, target: AccountRecord) -> None:
    """管理员只能操作权限等级不高于自己的账号，同级允许。"""

    if target.permission_level > actor.account.permission_level:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "target_level_denied",
            "cannot manage an account above your own level",
        )


def _set_session_cookie(
    response: Response,
    token: str,
    *,
    cookie_name: str,
    cookie_secure: bool,
    max_age: int,
) -> None:
    response.set_cookie(
        cookie_name,
        token,
        max_age=max_age,
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
        path="/",
    )


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _store_http_error(exc: Exception, status_code: int) -> HTTPException:
    code = getattr(exc, "code", "webui_store_error")
    return _http_error(status_code, code, str(exc))
