# FastMCP 工具契约

MCP Streamable HTTP 地址为 `/mcp`。工具名称不带 `knowledge_` 前缀。

## `add`

必填：`user_id`、`workspace_id`、`workspace_name`、`type`。前三项由调用方原样提供，工具不会自动生成 Workspace ID。

- `type=file`：还需 `file_name`、`content_base64`，建议传正确的 `mime_type`；禁止 `content`。
- `type=str`：还需 `content`；禁止文件字段。

可选 `callback_url`。返回 `{task_id,status}`。内容重复时同步报错，不创建任务。

## `list_workspaces`

必填：`user_id`。返回 Workspace 名称、ID、文件/字符串/资源数量、总字节数与时间。

## `list_files`

必填：`user_id`、`workspace_id`，且 Workspace 必须属于该用户。可选：`include_string_content`，默认 `false`。返回 `files`、`strings`、`stats`。

## `delete_file`

必填：`user_id`、`workspace_id`、`file_id`、`file_name`；可选 `callback_url`。返回异步任务；不支持删除单条字符串。

## `get_task`

必填：`task_id`、`user_id`。轮询到 `succeeded` 或 `failed`。公开响应不包含内部任务类型。

## `retrieve`

必填：`user_id`、`workspace_id`、`query`。可选：`top_k`，默认 10，最大 50。同步直接返回检索结果。
