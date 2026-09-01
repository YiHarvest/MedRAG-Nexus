"""WebUI Agent 内置工具。"""

from .administration import administration_tool_specs
from .knowledge import knowledge_tool_specs
from .read import read_tool_specs


def builtin_tool_specs():
    return (*read_tool_specs(), *knowledge_tool_specs(), *administration_tool_specs())


def build_default_agent_tool_registry():
    from ..registry import AgentToolRegistry

    return AgentToolRegistry(builtin_tool_specs())


__all__ = [
    "administration_tool_specs",
    "builtin_tool_specs",
    "build_default_agent_tool_registry",
    "knowledge_tool_specs",
    "read_tool_specs",
]
