---
name: medrag-nexus
description: 供 AgentHub 智能体通过 MedRAG-Nexus MCP 在指定用户和 Workspace 内新增文件或字符串、列出资源、检索知识、删除文件并跟踪异步任务。
---

# MedRAG-Nexus（AgentHub）

使用本技能时，只在调用方明确给出的 `user_id` 和 `workspace_id` 范围内操作。MCP 是面向受信任 AgentHub 的独立集成入口，不使用 REST 账号 Session，因此不要猜测、复用或跨用户传递 Workspace ID。

## 工具选择

- `list_workspaces`：按 `user_id` 获取 Workspace、稳定 ID 和容量统计。
- `add`：统一新增文件或字符串，不存在 `add_file` 或 `add_str` 工具。
- `list_files`：列出 Workspace 内文件、字符串和统计。
- `retrieve`：在一个 Workspace 内执行向量与 BM25 混合检索。
- `delete_file`：删除完整文件；当前不能删除单条字符串。
- `get_task`：跟踪新增和删除工具返回的异步任务。

## 新增资源

调用 `add` 时始终传入：

- `user_id`
- `workspace_id`
- `workspace_name`
- `type`：只能是 `file` 或 `str`

当 `type=file`：

- 传 `file_name`、`mime_type`、`content_base64`。
- 不传 `content`。
- 仅接受 PDF、TXT、DOCX，默认最大 50 MiB。

当 `type=str`：

- 只传 `content`，默认最大 10 MiB。
- 不传任何文件字段。
- 字符串原文保存为 JSONL，不生成 Markdown。

MCP 的 `user_id`、`workspace_id`、`workspace_name` 必须由调用方明确提供。不要让智能体自行猜测 Workspace ID。REST Backend API 的账号、知识域和 Workspace 创建规则不同，见接口契约。

`workspace_id` 是调用方提供的非空安全字符串标识符。若该 ID 已存在，传入的用户和名称必须与原绑定关系一致。

重复内容会同步返回冲突，并且不会创建任务。文件和字符串分别去重；文件名相同但内容不同可以作为两个独立文件新增。

## 跟踪异步任务

1. `add`、`delete_file` 都只调用一次并保存返回的 32 位 `task_id`。
2. 使用同一个 `user_id` 调用 `get_task`。
3. `queued`、`running` 均未完成；继续轮询。
4. 只在 `succeeded` 后向用户报告操作成功。
5. `failed` 时报告 `error.stage`、`error.code`、`error.message`、尝试次数，以及是否 `requires_repair`。

`add`、`delete_file` 可选传 `callback_url`。回调 HTTP POST 包含 `task_id`、`status`、`stage`、`progress`，结束时还包含 `result` 或 `error`。不要依据等待时间自行重提任务。

## 列表和检索

- `list_workspaces`、`list_files` 和 `retrieve` 同步直接返回结果，不生成任务 ID。
- `list_files(user_id, workspace_id, include_string_content=false)` 默认只包含字符串元数据，两个 ID 必须属于同一用户范围。
- 只有确实需要展示原文时才使用 `include_string_content=true`。
- 文件项包含永久 `file_id`、文件名、哈希、精确 `size_bytes` 和时间。
- 字符串没有 ID 或名称，以内容哈希标识。
- 删除文件前先调用 `list_files` 获取当前 `file_id` 与 `file_name`，两者必须原样传给 `delete_file`。
- 删除具有幂等语义；失败入库补偿已移除目标时，任务仍成功并返回 `already_absent=true`。
- 每个检索结果都包含对应文本块的 `chunk_id`，可用于唯一标识该 chunk。
- 检索结果 `source_type=str` 时会省略文件字段，这是正常响应，不要补造字段。

## 失败处理

- `duplicate_file` / `duplicate_text`：提示内容已经存在，不重试。
- `workspace_identity_conflict`：该 ID 已绑定其他用户或名称，停止写入并核对前端数据。
- `redis_unavailable`：服务暂不可提交写任务，可稍后重试。
- `file_busy` / `upload_in_progress`：查询活动任务或稍后重试。
- `workspace_requires_repair`：停止写入并通知维护人员。
- 检索 `degraded=true`：结果仍可用，但必须同时说明警告。

需要精确字段时阅读：

- [HTTP 接口契约](references/api-contract.md)
- [MCP 工具契约](references/mcp-tools.md)
