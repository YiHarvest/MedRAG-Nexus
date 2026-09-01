"""WebUI Agent 操作与临时制品的数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from medrag_nexus.core.models import APIModel

ActionStatus = Literal[
    "pending",
    "confirmed",
    "executing",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]
ActionRiskLevel = Literal["read", "write", "sensitive", "destructive"]
ConfirmationMode = Literal["click", "typed_text"]


class ActionTarget(APIModel):
    """创建待确认操作时记录的稳定目标身份。"""

    resource_type: str
    resource_id: str
    version: str | None = None
    display_name: str | None = None


class AgentAction(APIModel):
    """与账号绑定并持久化的确认及执行记录。"""

    action_id: str
    account_id: str
    conversation_id: str
    tool_name: str
    canonical_arguments: dict[str, Any]
    required_permissions: list[str] = Field(default_factory=list)
    target: ActionTarget | None = None
    risk_level: ActionRiskLevel
    confirmation_mode: ConfirmationMode = "click"
    status: ActionStatus
    idempotency_key: str | None = None
    result_summary: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    modified_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    executing_at: datetime | None = None
    completed_at: datetime | None = None


class ArtifactResourceRequirement(APIModel):
    """每个下载者都必须实时满足的资源级权限。"""

    resource_type: str
    resource_id: str
    required_permission: str


class AgentArtifact(APIModel):
    """Agent 生成的短期文件元数据。"""

    artifact_id: str
    owner_account_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    file_name: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    storage_path: str
    required_permissions: list[str] = Field(default_factory=list)
    resource_requirements: list[ArtifactResourceRequirement] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by_account_id: str | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value

    def is_available(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


class AgentArtifactResponse(APIModel):
    """可安全返回客户端的制品元数据，明确排除服务端存储路径。"""

    artifact_id: str
    owner_account_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    required_permissions: list[str] = Field(default_factory=list)
    resource_requirements: list[ArtifactResourceRequirement] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @classmethod
    def from_record(cls, artifact: AgentArtifact) -> AgentArtifactResponse:
        return cls.model_validate(artifact.model_dump(exclude={"storage_path", "revoked_by_account_id"}))


class ArtifactDownloadRecord(APIModel):
    download_id: str
    artifact_id: str
    account_id: str
    downloaded_at: datetime


class AnswerSource(APIModel):
    """导出回答中渲染的引用来源。"""

    title: str
    reference: str | None = None
    excerpt: str | None = None


class AnswerExportContent(APIModel):
    """Word 渲染器接收的临时内容，不会单独持久化。"""

    question: str
    answer: str
    sources: list[AnswerSource] = Field(default_factory=list)
    title: str = "知识助手回答"
    generated_by: str | None = None
    generated_at: datetime | None = None
