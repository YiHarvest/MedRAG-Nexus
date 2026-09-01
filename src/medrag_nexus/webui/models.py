"""WebUI 账号 API 与持久化层共用的数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from medrag_nexus.core.models import APIModel

LoginName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=64, pattern=r"^[\w.@-]+$"),
]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Password = Annotated[str, StringConstraints(min_length=3, max_length=256)]
GroupKey = Annotated[str, StringConstraints(pattern=r"^webui\.[a-z][a-z0-9_.-]*$")]
KnowledgeUserId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
PrincipalType = Literal["account", "group", "level"]
BindingEffect = Literal["allow", "deny"]


class AccountRecord(APIModel):
    account_id: str
    login_name: str
    display_name: str
    password_hash: str
    permission_level: int
    enabled: bool
    bound_user_id: str | None = None
    must_change_password: bool
    credential_version: int
    failed_login_count: int
    locked_until: datetime | None = None
    password_changed_at: datetime
    last_login_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    groups: list[str] = Field(default_factory=list)
    bound_user_ids: list[str] = Field(default_factory=list)


class AccountCapabilities(APIModel):
    can_update: bool
    can_reset_password: bool
    can_bind_user: bool
    protected: bool


class AccountResponse(APIModel):
    account_id: str
    login_name: str
    display_name: str
    permission_level: int
    enabled: bool
    bound_user_id: str | None = None
    must_change_password: bool
    password_changed_at: datetime
    last_login_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    groups: list[str]
    bound_user_ids: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    capabilities: AccountCapabilities | None = None

    @classmethod
    def from_record(cls, account: AccountRecord) -> AccountResponse:
        return cls(
            **account.model_dump(exclude={"password_hash", "credential_version", "failed_login_count", "locked_until"})
        )


class AccountListResponse(APIModel):
    accounts: list[AccountResponse]
    total: int


class RegisterRequest(APIModel):
    login_name: LoginName
    display_name: DisplayName
    password: Password

    @model_validator(mode="after")
    def reject_password_matching_login(self) -> RegisterRequest:
        if self.password.casefold() == self.login_name.casefold():
            raise ValueError("password must not equal login_name")
        return self


class LoginRequest(APIModel):
    login_name: LoginName
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ChangePasswordRequest(APIModel):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: Password

    @field_validator("new_password")
    @classmethod
    def reject_common_passwords(cls, value: str) -> str:
        if value.casefold() in {"password1234", "123456789012", "qwertyuiop12"}:
            raise ValueError("new_password is too common")
        return value


class AdminCreateAccountRequest(APIModel):
    login_name: LoginName
    display_name: DisplayName
    password: Password
    permission_level: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    group_keys: list[GroupKey] = Field(default_factory=list)
    must_change_password: bool = True

    @model_validator(mode="after")
    def reject_password_matching_login(self) -> AdminCreateAccountRequest:
        if self.password.casefold() == self.login_name.casefold():
            raise ValueError("password must not equal login_name")
        return self


class AdminPatchAccountRequest(APIModel):
    display_name: DisplayName | None = None
    permission_level: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    enabled: bool | None = None
    group_keys: list[GroupKey] | None = None
    must_change_password: bool | None = None

    @model_validator(mode="after")
    def reject_empty_patch(self) -> AdminPatchAccountRequest:
        values = self.model_dump(exclude_unset=True)
        if not values:
            raise ValueError("at least one field must be supplied")
        return self


class AdminResetPasswordRequest(APIModel):
    new_password: Password
    must_change_password: bool = True


class SessionResponse(APIModel):
    account: AccountResponse
    permissions: list[str]
    expires_at: datetime | None = None


class MessageResponse(APIModel):
    status: Literal["ok"] = "ok"


class AuditEventResponse(APIModel):
    event_id: str
    actor_account_id: str | None = None
    action: str
    resource_type: str
    resource_id: str
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    request_id: str | None = None
    created_at: datetime


class AuditEventListResponse(APIModel):
    events: list[AuditEventResponse]
    total: int


class PermissionPluginResponse(APIModel):
    plugin_id: str
    version: str
    requires: list[str]


class PermissionLevelResponse(APIModel):
    value: int
    name: str
    description: str
    permissions: list[str]


class PermissionNodeResponse(APIModel):
    key: str
    description: str
    plugin_id: str
    available: bool
    custom_assignable: bool


class PermissionGroupResponse(APIModel):
    key: str
    name: str
    description: str
    permissions: list[str]
    system_managed: bool


class PermissionCatalogResponse(APIModel):
    plugins: list[PermissionPluginResponse]
    levels: list[PermissionLevelResponse]
    nodes: list[PermissionNodeResponse]
    groups: list[PermissionGroupResponse]


class PermissionGroupListResponse(APIModel):
    groups: list[PermissionGroupResponse]
    total: int


class CreatePermissionGroupRequest(APIModel):
    group_key: GroupKey
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] = ""
    permissions: list[GroupKey]

    @field_validator("permissions")
    @classmethod
    def normalize_permissions(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class PatchPermissionGroupRequest(APIModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] | None = None
    permissions: list[GroupKey] | None = None

    @field_validator("permissions")
    @classmethod
    def normalize_permissions(cls, value: list[str] | None) -> list[str] | None:
        return sorted(set(value)) if value is not None else None

    @model_validator(mode="after")
    def reject_empty_patch(self) -> PatchPermissionGroupRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self
