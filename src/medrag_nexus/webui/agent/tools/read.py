"""WebUI Agent 只读工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from medrag_nexus.core.models import RetrievalRequest
from medrag_nexus.services.files import FileService
from medrag_nexus.services.health import dependency_health
from medrag_nexus.services.retrieval import retrieve

from ..context import AgentAuthorizationError, AgentContext, _jsonable
from ..registry import ToolSpec, object_schema


def _string(description: str, *, max_length: int = 128) -> dict[str, Any]:
    return {"type": "string", "description": description, "minLength": 1, "maxLength": max_length}


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(arguments: Mapping[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when provided")
    return value.strip()


async def list_workspaces(context: AgentContext, _arguments: Mapping[str, Any]) -> dict[str, Any]:
    principal = context.principal
    permissions = set(principal.permissions)
    visible_users: list[dict[str, Any]] = []
    visible_workspaces: list[dict[str, Any]] = []
    for user in (await context.runtime.metadata.list_users()).users:
        if not await context.policies.allows_user(
            principal.account,
            permissions,
            user_id=user.user_id,
            action="webui.user.read",
            permission="webui.user.read",
        ):
            continue
        visible_users.append(_jsonable(user))
        for summary in (await context.runtime.metadata.list_workspaces(user.user_id)).workspaces:
            if await context.policies.allows_workspace(
                principal.account,
                permissions,
                workspace_id=summary.workspace_id,
                action="webui.workspace.read",
                permission="webui.workspace.read",
                user_id=user.user_id,
            ):
                visible_workspaces.append({**_jsonable(summary), "user_id": user.user_id})
    return {"users": visible_users, "workspaces": visible_workspaces}


async def list_files(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    requested_workspace_id = _optional_string(arguments, "workspace_id")
    include_text_content = bool(arguments.get("include_text_content", False))

    async def load(user_id: str, workspace_id: str, workspace_name: str | None = None) -> dict[str, Any]:
        response = await FileService(context.runtime).list_files(
            user_id,
            workspace_id,
            include_string_content=include_text_content,
        )
        # list_files 已经从受信任的服务端元数据返回了本轮真实 file_id；下载时再核对
        # 名称、内容哈希和实时权限即可，无需让模型额外调用一次详情工具。
        downloadable = await context.allows_workspace(workspace_id, "webui.resource.file.download")
        verified_files = context.metadata.setdefault("verified_download_files", {})
        if isinstance(verified_files, dict):
            for item in response.files:
                verified_files[(workspace_id, item.file_id)] = {
                    "content_hash": item.content_hash,
                    "file_name": item.file_name,
                    "downloadable": downloadable,
                }
        payload = _jsonable(response)
        return {**payload, "workspace_name": workspace_name} if workspace_name else payload

    if requested_workspace_id is not None:
        workspace = await context.runtime.metadata.get_workspace(requested_workspace_id)
        if workspace is not None and await context.allows_workspace(
            requested_workspace_id,
            "webui.workspace.read",
        ):
            return await load(workspace.user_id, requested_workspace_id)

    # workspace_id 未提供或已失效时，不让模型继续猜 ID；改为列出当前账号全部
    # 可见知识库。权限范围没有扩大，且每个下载仍会实时重新鉴权。
    visible = await list_workspaces(context, {})
    responses = await asyncio.gather(
        *(
            load(
                str(item["user_id"]),
                str(item["workspace_id"]),
                str(item.get("workspace_name") or "") or None,
            )
            for item in visible["workspaces"]
        )
    )
    recovered = requested_workspace_id is not None
    return {
        "requested_workspace_id": requested_workspace_id,
        "workspace_id_recovered": recovered,
        "workspaces": responses,
        "message": (
            "传入的 workspace_id 不可用；已改为列出当前账号全部可见知识库，请从结果使用真实 ID。"
            if recovered
            else "已列出当前账号全部可见知识库中的文件。"
        ),
    }


async def retrieve_user_knowledge(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """检索当前同时通过知识域和知识库 ACL 授权的全部知识库。"""

    query = _required_string(arguments, "query")
    if len(query) > 4_000:
        raise ValueError("query is too long")
    raw_top_k = arguments.get("top_k", 8)
    if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
        raise ValueError("top_k must be an integer")
    top_k = max(1, min(20, raw_top_k))
    visible = await list_workspaces(context, {})
    workspaces = [await context.runtime.metadata.get_workspace(item["workspace_id"]) for item in visible["workspaces"]]
    workspaces = [workspace for workspace in workspaces if workspace is not None]
    if not workspaces:
        return {"query": query, "matches": [], "citations": [], "message": "当前账号没有可检索的知识库。"}
    semaphore = asyncio.Semaphore(context.runtime.settings.chat_workspace_concurrency)

    async def search(workspace: Any) -> Any:
        async with semaphore:
            return await asyncio.wait_for(
                retrieve(
                    context.runtime,
                    RetrievalRequest(
                        user_id=workspace.user_id,
                        workspace_id=workspace.workspace_id,
                        query=query,
                        top_k=top_k,
                    ),
                ),
                timeout=context.runtime.settings.llm_timeout_seconds,
            )

    results = await asyncio.gather(*(search(workspace) for workspace in workspaces), return_exceptions=True)
    names = {workspace.workspace_id: workspace.workspace_name for workspace in workspaces}
    items: list[Any] = []
    warnings: list[str] = []
    for workspace, result in zip(workspaces, results, strict=True):
        if isinstance(result, Exception):
            warnings.append(f"{workspace.workspace_name} 检索失败")
            continue
        items.extend(result.items)
        warnings.extend(warning.message for warning in result.warnings)

    def score(item: Any) -> float:
        for value in (item.scores.rerank, item.scores.rrf, item.scores.bm25, item.scores.vector):
            if value is not None:
                return float(value)
        return -float(item.rank)

    selected: list[Any] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sorted(items, key=score, reverse=True):
        key = (item.workspace_id, str(item.file_id or ""), str(item.chunk_id))
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= top_k:
            break
    citations = [
        {
            "citation_id": index,
            "workspace_id": item.workspace_id,
            "workspace_name": names.get(item.workspace_id, item.workspace_id),
            "source_type": item.source_type,
            "file_id": item.file_id,
            "file_name": item.file_name,
            "chunk_id": str(item.chunk_id),
            "section": item.section,
            "page_number": item.page_number,
            "excerpt": item.content[:500],
        }
        for index, item in enumerate(selected, start=1)
    ]
    return {
        "query": query,
        "matches": [
            {
                "citation": f"[{index}]",
                "workspace": citation["workspace_name"],
                "file": citation["file_name"] or "文本内容",
                "section": citation["section"],
                "page_number": citation["page_number"],
                "content": selected[index - 1].content[:1600],
            }
            for index, citation in enumerate(citations, start=1)
        ],
        "citations": citations,
        "warnings": warnings,
    }


async def get_file_details(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    workspace_id = _required_string(arguments, "workspace_id")
    file_id = _required_string(arguments, "file_id")
    await context.require_workspace(workspace_id, "webui.workspace.read")
    resource = await context.runtime.metadata.get_file(workspace_id, file_id)
    if resource is None or resource.file_name is None:
        raise AgentAuthorizationError("file_not_found", "文件不存在；请先调用 list_files 获取当前真实 file_id。")
    downloadable = await context.allows_workspace(workspace_id, "webui.resource.file.download")
    verified_files = context.metadata.setdefault("verified_download_files", {})
    if isinstance(verified_files, dict):
        verified_files[(workspace_id, file_id)] = {
            "content_hash": resource.content_hash,
            "file_name": resource.file_name,
            "downloadable": downloadable,
        }
    return {
        "workspace_id": workspace_id,
        "file_id": file_id,
        "file_name": resource.file_name,
        "mime_type": resource.mime_type,
        "content_hash": resource.content_hash,
        "markdown_hash": resource.markdown_hash,
        "size_bytes": resource.size_bytes,
        "parser": resource.parser,
        "degraded": resource.degraded,
        "chunk_count": resource.chunk_count,
        "created_at": resource.created_at.isoformat(),
        "modified_at": resource.modified_at.isoformat(),
        "downloadable": downloadable,
    }


async def prepare_file_download(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    workspace_id = _required_string(arguments, "workspace_id")
    file_id = _required_string(arguments, "file_id")
    verified_files = context.metadata.get("verified_download_files", {})
    verification = verified_files.get((workspace_id, file_id)) if isinstance(verified_files, dict) else None
    if not isinstance(verification, Mapping) or verification.get("downloadable") is not True:
        raise AgentAuthorizationError(
            "file_verification_required",
            "下载前必须先调用 list_files 获取并登记本轮真实 workspace_id 和 file_id。",
        )
    await context.require_workspace(workspace_id, "webui.resource.file.download")
    resource = await context.runtime.metadata.get_file(workspace_id, file_id)
    if resource is None or resource.file_name is None:
        verified_files.pop((workspace_id, file_id), None)
        raise AgentAuthorizationError("file_not_found", "文件不存在；请重新调用 list_files 获取当前真实 file_id。")
    if (
        verification.get("content_hash") != resource.content_hash
        or verification.get("file_name") != resource.file_name
    ):
        verified_files.pop((workspace_id, file_id), None)
        raise AgentAuthorizationError(
            "file_verification_required",
            "文件在验证后发生变化；请重新调用 list_files 获取当前文件信息。",
        )
    return await context.invoke_capability(
        "prepare_file_download",
        {"workspace_id": workspace_id, "file_id": file_id, "file_name": resource.file_name},
    )


async def export_answer_to_word(context: AgentContext, arguments: Mapping[str, Any]) -> Any:
    answer = _required_string(arguments, "answer")
    payload = dict(arguments)
    payload["answer"] = answer
    return await context.invoke_capability("export_answer_to_word", payload)


async def list_accounts(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    limit = min(max(int(arguments.get("limit", 100)), 1), 500)
    accounts = await context.store.list_accounts()
    items = []
    for account in accounts[:limit]:
        item = account.model_dump(
            mode="json",
            exclude={"password_hash", "credential_version", "failed_login_count", "locked_until"},
        )
        item["permissions"] = sorted(await context.store.permission_keys(account.account_id))
        items.append(item)
    return {"accounts": items, "total": len(accounts)}


async def permission_catalog(context: AgentContext, _arguments: Mapping[str, Any]) -> Any:
    return _jsonable(await context.store.permission_catalog())


async def list_permission_groups(context: AgentContext, _arguments: Mapping[str, Any]) -> dict[str, Any]:
    groups = await context.store.list_permission_groups()
    return {"groups": [_jsonable(group) for group in groups], "total": len(groups)}


async def list_audit_events(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    limit = min(max(int(arguments.get("limit", 100)), 1), 500)
    offset = max(int(arguments.get("offset", 0)), 0)
    events, total = await context.store.list_audit_events(limit=limit, offset=offset)
    return {"events": [_jsonable(event) for event in events], "total": total}


async def get_user_permission_config(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    user_id = _required_string(arguments, "user_id")
    await context.require_user(user_id, "webui.user.policy.manage")
    policy = await context.policies.get_user_policy(user_id)
    bindings = await context.policies.list_bindings(
        "user",
        user_id,
        (
            "webui.user.read",
            "webui.workspace.create",
            "webui.user.rename",
            "webui.user.delete",
            "webui.user.policy.manage",
        ),
    )
    return {
        "policy": _jsonable(policy),
        "bindings": {key: [_jsonable(v) for v in values] for key, values in bindings.items()},
    }


async def get_workspace_permission_config(context: AgentContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    workspace_id = _required_string(arguments, "workspace_id")
    await context.require_workspace(workspace_id, "webui.workspace.policy.manage")
    policy = await context.policies.get_workspace_policy(workspace_id)
    actions = (
        "webui.workspace.read",
        "webui.workspace.rename",
        "webui.workspace.delete",
        "webui.workspace.policy.manage",
        "webui.resource.file.add",
        "webui.resource.file.download",
        "webui.resource.file.delete",
        "webui.resource.text.add",
        "webui.resource.text.delete",
    )
    bindings = await context.policies.list_bindings("workspace", workspace_id, actions)
    return {
        "policy": _jsonable(policy),
        "bindings": {key: [_jsonable(v) for v in values] for key, values in bindings.items()},
    }


async def system_health(context: AgentContext, _arguments: Mapping[str, Any]) -> Any:
    return _jsonable(await dependency_health(context.runtime))


def read_tool_specs() -> tuple[ToolSpec, ...]:
    workspace_id = _string("知识库 ID")
    file_id = _string("文件 ID")
    return (
        ToolSpec(
            "retrieve_user_knowledge",
            "在当前账号实时获授权的全部知识库中检索相关内容。",
            object_schema(
                {
                    "query": _string("检索问题", max_length=4_000),
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                required=("query",),
            ),
            retrieve_user_knowledge,
            ("webui.retrieval.use", "webui.user.read", "webui.workspace.read"),
        ),
        ToolSpec(
            "list_workspaces",
            "列出当前账号实时获授权的知识域和知识库。",
            object_schema(),
            list_workspaces,
            ("webui.user.read", "webui.workspace.read"),
        ),
        ToolSpec(
            "list_files",
            "列出获授权知识库内的文件和文本资源；workspace_id 未知时应省略，服务端会安全列出全部可见知识库。",
            object_schema(
                {"workspace_id": workspace_id, "include_text_content": {"type": "boolean", "default": False}},
            ),
            list_files,
            ("webui.workspace.read",),
        ),
        ToolSpec(
            "get_file_details",
            "使用 list_files 返回的真实 workspace_id 和 file_id 查看文件详情；查看详情后也可直接准备下载。",
            object_schema({"workspace_id": workspace_id, "file_id": file_id}, required=("workspace_id", "file_id")),
            get_file_details,
            ("webui.workspace.read",),
        ),
        ToolSpec(
            "prepare_file_download",
            "为本轮 list_files 返回（或 get_file_details 验证）且 downloadable=true 的原始文件创建安全下载响应；"
            "获得唯一 file_id 后应立即调用，不得猜测 ID。",
            object_schema({"workspace_id": workspace_id, "file_id": file_id}, required=("workspace_id", "file_id")),
            prepare_file_download,
            ("webui.resource.file.download",),
            input_mode="model",
        ),
        ToolSpec(
            "export_answer_to_word",
            "将当前回答和引用导出为保留 24 小时的 Word 制品。",
            object_schema(
                {
                    "answer": _string("需要导出的回答正文", max_length=200_000),
                    "question": {"type": "string", "maxLength": 20_000},
                    "sources": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
                    "required_permissions": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    "required_resources": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
                },
                required=("answer",),
            ),
            export_answer_to_word,
            ("webui.agent.export",),
            risk_level="write",
        ),
        ToolSpec(
            "list_accounts",
            "列出可管理的 WebUI 账号（绝不返回密码字段）。",
            object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
            list_accounts,
            ("webui.account.manage",),
        ),
        ToolSpec(
            "get_permission_catalog",
            "读取当前权限节点、等级和插件目录。",
            object_schema(),
            permission_catalog,
            ("webui.permission.catalog.read",),
        ),
        ToolSpec(
            "list_permission_groups",
            "列出权限组及其权限节点。",
            object_schema(),
            list_permission_groups,
            ("webui.permission.catalog.read",),
        ),
        ToolSpec(
            "list_audit_events",
            "分页读取 WebUI 安全审计。",
            object_schema(
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    "offset": {"type": "integer", "minimum": 0},
                }
            ),
            list_audit_events,
            ("webui.audit.read",),
        ),
        ToolSpec(
            "get_user_permission_config",
            "读取指定知识域的策略与 ACL 配置。",
            object_schema({"user_id": _string("知识域 ID")}, required=("user_id",)),
            get_user_permission_config,
            ("webui.user.policy.manage",),
        ),
        ToolSpec(
            "get_workspace_permission_config",
            "读取指定知识库的策略与 ACL 配置。",
            object_schema({"workspace_id": workspace_id}, required=("workspace_id",)),
            get_workspace_permission_config,
            ("webui.workspace.policy.manage",),
        ),
        ToolSpec("get_system_health", "查看系统依赖健康状态。", object_schema(), system_health, ("webui.system.read",)),
    )
