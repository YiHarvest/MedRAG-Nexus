"""通过 FastMCP 暴露 JD Knowledge 的知识管理与检索工具。"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from jd_knowledge.core.models import AddRequest, DeleteFileRequest, FileSource, RetrievalRequest, StringSource
from jd_knowledge.services.files import FileService
from jd_knowledge.services.retrieval import retrieve
from jd_knowledge.services.runtime import Runtime

mcp = FastMCP(
    name="Knowledge",
    instructions="面向 AgentHub 的 Workspace 知识新增、列表、检索、删除和异步任务查询工具。",
)
_runtime: Runtime | None = None


def bind_runtime(runtime: Runtime) -> None:
    """绑定由主应用创建并共享给 MCP 工具的运行时。"""

    global _runtime
    _runtime = runtime


def runtime() -> Runtime:
    """返回已绑定的运行时，未初始化时给出明确错误。"""

    if _runtime is None:
        raise RuntimeError("MCP runtime has not been initialized by main.py")
    return _runtime


def _dump(value: Any) -> dict[str, Any]:
    """将 Pydantic 响应转换为适合 MCP 传输的 JSON 字典。"""

    return value.model_dump(mode="json", exclude_none=True)


async def _mcp_log(level: str, message: str, *, tool: str, **context: object) -> None:
    """MCP 工具调用与 HTTP API 使用同一套终端及文件日志。"""
    task_log = getattr(runtime(), "task_log", None)
    if task_log is None:
        return
    try:
        await task_log.write_api(level, message, transport="mcp", tool=tool, **context)
    except Exception:
        return


@mcp.tool(name="add")
async def add(
    user_id: str,
    workspace_id: str,
    workspace_name: str,
    type: Literal["file", "str"],
    file_name: str | None = None,
    mime_type: str | None = None,
    content_base64: str | None = None,
    content: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """异步新增一个文件或字符串；type=file 与 type=str 的字段不可混用。"""
    await _mcp_log(
        "INFO",
        "MCP 新增知识调用开始",
        tool="add",
        user_id=user_id,
        workspace_id=workspace_id,
        source_type=type,
        file_name=file_name,
    )
    if type == "file":
        if not file_name or not content_base64 or content is not None:
            raise ValueError("type=file requires file_name and content_base64, and forbids content")
        source = FileSource(
            file_name=file_name,
            mime_type=mime_type or "application/octet-stream",
            content_base64=content_base64,
        )
    else:
        if content is None or file_name is not None or content_base64 is not None or mime_type is not None:
            raise ValueError("type=str requires content and forbids file fields")
        source = StringSource(content=content)
    request = AddRequest(
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        source=source,
        callback_url=callback_url,
    )
    response = await FileService(runtime()).submit_add(request)
    await _mcp_log("INFO", "MCP 新增知识任务提交成功", tool="add", task_id=response.task_id)
    return _dump(response)


@mcp.tool(name="list_workspaces")
async def list_workspaces(user_id: str) -> dict[str, Any]:
    """同步列出用户的全部 Workspace 和资源统计。"""
    await _mcp_log("INFO", "MCP Workspace 列表查询开始", tool="list_workspaces", user_id=user_id)
    response = await FileService(runtime()).list_workspaces(user_id)
    await _mcp_log(
        "INFO",
        "MCP Workspace 列表查询完成",
        tool="list_workspaces",
        user_id=user_id,
        workspace_count=len(response.workspaces),
    )
    return _dump(response)


@mcp.tool(name="list_files")
async def list_files(
    user_id: str,
    workspace_id: str,
    include_string_content: bool = False,
) -> dict[str, Any]:
    """同步列出 Workspace 资源；可选择包含完整字符串。"""
    await _mcp_log(
        "INFO",
        "MCP Workspace 资源列表查询开始",
        tool="list_files",
        user_id=user_id,
        workspace_id=workspace_id,
        include_string_content=include_string_content,
    )
    response = await FileService(runtime()).list_files(
        user_id,
        workspace_id,
        include_string_content=include_string_content,
    )
    await _mcp_log(
        "INFO",
        "MCP Workspace 资源列表查询完成",
        tool="list_files",
        workspace_id=workspace_id,
        file_count=len(response.files),
        string_count=len(response.strings),
    )
    return _dump(response)


@mcp.tool(name="delete_file")
async def delete_file(
    user_id: str,
    workspace_id: str,
    file_id: str,
    file_name: str,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """异步删除一个文件；字符串暂不支持删除。"""
    await _mcp_log(
        "INFO",
        "MCP 删除文件调用开始",
        tool="delete_file",
        user_id=user_id,
        workspace_id=workspace_id,
        file_id=file_id,
        file_name=file_name,
        callback_url=callback_url,
    )
    request = DeleteFileRequest(
        user_id=user_id,
        workspace_id=workspace_id,
        file_id=file_id,
        file_name=file_name,
        callback_url=callback_url,
    )
    response = await FileService(runtime()).submit_delete(request)
    await _mcp_log("INFO", "MCP 删除文件任务提交成功", tool="delete_file", task_id=response.task_id)
    return _dump(response)


@mcp.tool(name="get_task")
async def get_task(task_id: str, user_id: str) -> dict[str, Any]:
    """查询 add 或 delete_file 异步任务的最终状态和失败阶段。"""
    await _mcp_log("INFO", "MCP 任务状态查询开始", tool="get_task", task_id=task_id, user_id=user_id)
    response = await FileService(runtime()).get_task(task_id, user_id)
    await _mcp_log(
        "INFO",
        "MCP 任务状态查询完成",
        tool="get_task",
        task_id=task_id,
        status=response.status.value,
        stage=response.stage,
        progress=f"{response.progress.percent}%",
    )
    return _dump(response)


@mcp.tool(name="retrieve")
async def retrieve_knowledge(
    user_id: str,
    workspace_id: str,
    query: str,
    top_k: int | None = None,
) -> dict[str, Any]:
    """同步执行向量与关键词混合检索并直接返回结果。"""
    await _mcp_log(
        "INFO",
        "MCP 知识检索调用开始",
        tool="retrieve",
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
        top_k=top_k,
    )
    request = RetrievalRequest(
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
        top_k=top_k,
    )
    response = await retrieve(runtime(), request)
    await _mcp_log(
        "INFO",
        "MCP 知识检索调用完成",
        tool="retrieve",
        workspace_id=workspace_id,
        count=response.count,
        degraded=response.degraded,
    )
    return _dump(response)


mcp_http_app = mcp.http_app(path="/mcp", stateless_http=True, transport="http")
