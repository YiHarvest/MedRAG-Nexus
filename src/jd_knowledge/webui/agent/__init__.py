"""与 WebUI 权限等价的 Agent 基础设施。"""

from .artifacts import ArtifactService, UnsafeArtifactPathError
from .context import AgentAuthorizationError, AgentContext
from .exports import WORD_MIME_TYPE, export_answer_to_word, render_answer_docx
from .models import (
    ActionRiskLevel,
    ActionStatus,
    ActionTarget,
    AgentAction,
    AgentArtifact,
    AgentArtifactResponse,
    AnswerExportContent,
    AnswerSource,
    ArtifactDownloadRecord,
    ArtifactResourceRequirement,
    ConfirmationMode,
)
from .registry import AgentToolError, AgentToolRegistry, ToolSpec
from .store import (
    ActionNotFoundError,
    ActionOwnershipError,
    ActionPayloadError,
    ActionStateError,
    AgentStore,
    AgentStoreError,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    IdempotencyConflictError,
    InvalidConfirmationError,
    canonical_arguments,
)
from .tools import build_default_agent_tool_registry, builtin_tool_specs


def build_agent_tool_registry() -> AgentToolRegistry:
    return build_default_agent_tool_registry()


__all__ = [
    "AgentAuthorizationError",
    "ActionNotFoundError",
    "ActionOwnershipError",
    "ActionPayloadError",
    "ActionRiskLevel",
    "ActionStateError",
    "ActionStatus",
    "ActionTarget",
    "AgentAction",
    "AgentArtifact",
    "AgentArtifactResponse",
    "AgentContext",
    "AgentStore",
    "AgentStoreError",
    "AgentToolError",
    "AgentToolRegistry",
    "AnswerExportContent",
    "AnswerSource",
    "ArtifactDownloadRecord",
    "ArtifactNotFoundError",
    "ArtifactResourceRequirement",
    "ArtifactService",
    "ArtifactUnavailableError",
    "ConfirmationMode",
    "IdempotencyConflictError",
    "InvalidConfirmationError",
    "ToolSpec",
    "UnsafeArtifactPathError",
    "WORD_MIME_TYPE",
    "build_agent_tool_registry",
    "build_default_agent_tool_registry",
    "builtin_tool_specs",
    "canonical_arguments",
    "export_answer_to_word",
    "render_answer_docx",
]
