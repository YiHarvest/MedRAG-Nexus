"""定义 API、持久化记录与领域错误使用的共享数据模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

# 通用业务标识符，目前主要用于 user_id：
# - 校验前自动去除首尾空白，避免同一标识因误输入空格而产生不同记录；
# - 长度限制为 1～128 个字符；
# - 允许字母、数字、下划线等单词字符，以及点号、@、冒号和连字符，兼容用户名、邮箱和外部系统 ID。
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[\w.@:-]+$"),
]

# Workspace 的业务标识符，由调用方提供，后端不会根据用户或 Workspace 名称重新生成：
# - 校验前自动去除首尾空白，长度限制为 1～128 个字符；
# - 仅允许单词字符、冒号、@ 和连字符，禁止路径分隔符等可能影响存储键安全的字符。
WorkspaceId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[\w:@-]+$"),
]

# 文件资源的永久标识符：固定使用 ``file_`` 前缀，后接规范的小写 UUID v4；
# UUID 的版本位必须为 4，变体位必须为 8、9、a 或 b，从而拒绝任意字符串或其他版本 UUID。
FileId = Annotated[
    str,
    StringConstraints(pattern=r"^file_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]

# 异步任务标识符：使用 UUID 的 ``hex`` 表示形式，即恰好 32 位小写十六进制字符且不包含连字符。
TaskId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{32}$")]


def local_now() -> datetime:
    """返回带本地时区信息的当前时间。"""

    return datetime.now().astimezone()


class APIModel(BaseModel):
    """禁止未声明字段的 API 数据模型基类。"""

    model_config = ConfigDict(extra="forbid")


class FileSource(APIModel):
    """以 Base64 内容表示的待新增文件来源。"""

    type: Literal["file"] = "file"
    file_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    mime_type: Annotated[str, StringConstraints(min_length=1, max_length=127)] = "application/octet-stream"
    content_base64: Annotated[str, StringConstraints(min_length=1)]


class StringSource(APIModel):
    """待新增的非空字符串知识来源。"""

    type: Literal["str"] = "str"
    content: Annotated[str, StringConstraints(min_length=1)]

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白字符的字符串内容。"""

        if not value.strip():
            raise ValueError("content must not be blank")
        return value


AddSource = Annotated[FileSource | StringSource, Field(discriminator="type")]


class AddRequest(APIModel):
    """新增文件或字符串知识的请求。"""

    user_id: Identifier
    workspace_id: WorkspaceId
    workspace_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    source: AddSource
    callback_url: AnyHttpUrl | None = None


class DeleteFileRequest(APIModel):
    """从指定 Workspace 删除文件知识的请求。"""

    user_id: Identifier
    workspace_id: WorkspaceId
    file_id: FileId
    file_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    callback_url: AnyHttpUrl | None = None


class DeleteStringRequest(APIModel):
    """从指定 Workspace 删除字符串知识。"""

    user_id: Identifier
    workspace_id: WorkspaceId
    content_hash: ContentHash
    callback_url: AnyHttpUrl | None = None


class TaskAccepted(APIModel):
    """异步任务被队列接受后的响应。"""

    task_id: TaskId
    status: Literal["queued"] = "queued"


class TaskStatus(str, Enum):
    """异步任务的生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskProgress(APIModel):
    """异步任务的数值进度快照。"""

    current: int = 0
    total: int = 100
    percent: float = 0.0


class TaskError(APIModel):
    """异步任务失败时记录的结构化错误。"""

    code: str
    stage: str
    message: str
    attempts: int = 1
    requires_repair: bool = False
    compensation_error: str | None = None


class TaskRecord(APIModel):
    """用于内部持久化和恢复的完整任务记录。"""

    task_id: TaskId
    user_id: str
    workspace_id: str
    workspace_name: str
    operation: Literal[
        "add_file",
        "add_str",
        "delete_file",
        "delete_string",
        "list_workspaces",
        "list_files",
        "retrieval",
    ]
    status: TaskStatus = TaskStatus.QUEUED
    stage: str = "queued"
    progress: TaskProgress = Field(default_factory=TaskProgress)
    payload: dict[str, Any] = Field(default_factory=dict)
    journal: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: TaskError | None = None
    created_at: datetime = Field(default_factory=local_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    modified_at: datetime = Field(default_factory=local_now)


class TaskResponse(APIModel):
    """向客户端公开的异步任务状态。"""

    task_id: TaskId
    status: TaskStatus
    stage: str
    progress: TaskProgress
    result: dict[str, Any] | None = None
    error: TaskError | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    modified_at: datetime


class WorkspaceStats(APIModel):
    """Workspace 中各类资源的聚合统计。"""

    resource_count: int = 0
    file_count: int = 0
    str_count: int = 0
    total_size_bytes: int = 0


class WorkspaceRecord(APIModel):
    """持久化的 Workspace 元数据与资源统计。"""

    workspace_id: WorkspaceId
    user_id: str
    workspace_name: str
    resource_count: int = 0
    file_count: int = 0
    str_count: int = 0
    total_size_bytes: int = 0
    created_at: datetime = Field(default_factory=local_now)
    modified_at: datetime = Field(default_factory=local_now)


class WorkspaceListItem(APIModel):
    """Workspace 列表中的单个摘要项。"""

    workspace_id: WorkspaceId
    workspace_name: str
    resource_count: int
    file_count: int
    str_count: int
    total_size_bytes: int
    created_at: datetime
    modified_at: datetime


class WorkspaceListResponse(APIModel):
    """指定用户可见的 Workspace 列表响应。"""

    user_id: str
    workspaces: list[WorkspaceListItem]


class UserListItem(APIModel):
    """WebUI 用户选择器中的用户摘要。"""

    user_id: Identifier
    user_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    workspace_count: int = 0
    resource_count: int = 0
    file_count: int = 0
    str_count: int = 0
    total_size_bytes: int = 0


class UserListResponse(APIModel):
    """SQLite 中已有用户的列表。"""

    users: list[UserListItem]


class UserCreateRequest(APIModel):
    """知识域记录；REST 创建时 user_id 默认由后端生成。"""

    user_id: Identifier
    user_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class FileListItem(APIModel):
    """资源列表中的文件知识摘要。"""

    file_id: FileId
    file_name: str
    content_hash: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime


class StringListItem(APIModel):
    """资源列表中的字符串知识摘要。"""

    content: str | None = None
    content_hash: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime


class FileListResponse(APIModel):
    """Workspace 文件、字符串及其统计的列表响应。"""

    workspace_id: WorkspaceId
    files: list[FileListItem]
    strings: list[StringListItem]
    stats: WorkspaceStats


class ResourceRecord(APIModel):
    """已摄取知识资源的持久化元数据。"""

    row_id: int | None = None
    document_id: UUID
    workspace_id: WorkspaceId
    user_id: str
    workspace_name: str
    source_type: Literal["file", "str"]
    file_id: FileId | None = None
    file_name: str | None = None
    mime_type: str | None = None
    content_hash: str
    size_bytes: int
    markdown_hash: str | None = None
    parser: str
    degraded: bool = False
    chunk_count: int
    artifact_path: str
    created_at: datetime = Field(default_factory=local_now)
    modified_at: datetime = Field(default_factory=local_now)


class ChunkRecord(APIModel):
    """用于索引和检索的单个知识文本块。"""

    chunk_id: UUID
    workspace_id: WorkspaceId
    user_id: str
    document_id: UUID
    source_type: Literal["file", "str"]
    file_id: FileId | None = None
    file_name: str | None = None
    ordinal: int
    content: str
    content_hash: str
    section: str | None = None
    page_number: int | None = None
    chunk_type: Literal["paragraph", "table", "list", "code"] = "paragraph"
    start_offset: int
    end_offset: int
    embedding_text: str
    vector: list[float] | None = Field(default=None, exclude=True)
    created_at: datetime = Field(default_factory=local_now)


class RetrievalRequest(APIModel):
    """在指定 Workspace 中执行知识检索的请求。"""

    user_id: Identifier
    workspace_id: WorkspaceId
    query: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    top_k: int | None = Field(default=None, ge=1)


class ChatMessage(APIModel):
    """聊天请求中的一条可见消息。"""

    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class ChatRequest(APIModel):
    """面向指定用户全部 Workspace 的流式聊天请求。"""

    user_id: Identifier
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    top_k: int = Field(default=8, ge=1, le=20)
    conversation_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_latest_user_message(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the latest chat message must have role=user")
        return self


class RetrievalScores(APIModel):
    """检索项在各召回和重排阶段获得的分数。"""

    vector: float | None = None
    bm25: float | None = None
    rrf: float | None = None
    rerank: float | None = None


class RetrievalItem(APIModel):
    """包含来源、正文和评分信息的单条检索结果。"""

    rank: int
    chunk_id: UUID
    user_id: str
    workspace_id: WorkspaceId
    source_type: Literal["file", "str"]
    file_id: FileId | None = None
    file_name: str | None = None
    content: str
    section: str | None = None
    page_number: int | None = None
    scores: RetrievalScores
    matched_by: list[Literal["vector", "bm25"]]


class WarningItem(APIModel):
    """检索降级或非致命问题的警告。"""

    code: str
    message: str


class RetrievalResponse(APIModel):
    """混合知识检索的有序结果响应。"""

    query: str
    top_k: int = 10
    count: int = 3
    degraded: bool = False
    warnings: list[WarningItem] = Field(default_factory=list)
    items: list[RetrievalItem]


class DependencyState(APIModel):
    """单个外部依赖的健康状态与响应耗时。"""

    status: Literal["ok", "degraded", "unavailable"]
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(APIModel):
    """服务及其依赖的综合健康检查响应。"""

    status: Literal["ok", "degraded", "unavailable"]
    dependencies: dict[str, DependencyState] | None = None


class ErrorBody(APIModel):
    """API 错误响应中的结构化错误详情。"""

    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ErrorResponse(APIModel):
    """HTTP API 的统一错误响应包装。"""

    error: ErrorBody


class DomainError(Exception):
    """携带 HTTP 状态和机器可读代码的领域异常。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400, details: dict[str, Any] | None = None):
        """初始化领域异常及可选错误详情。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class FileBusyError(DomainError):
    """文件已有活动写任务时抛出的冲突异常。"""

    def __init__(self, active_task_id: str):
        """使用冲突任务标识初始化异常。"""

        super().__init__(
            "file_busy",
            "the file already has an active write task",
            status_code=409,
            details={"active_task_id": active_task_id},
        )
