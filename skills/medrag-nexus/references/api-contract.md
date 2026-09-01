# HTTP 接口摘要

基础地址默认 `http://127.0.0.1:28111`，所有业务路径以 `/api/v1` 开头，不提供旧 `/app/v1` 别名，也不做鉴权。

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| POST | `/add` | multipart 新增文件/字符串，返回 `202 {task_id,status}` |
| GET | `/users/{user_id}/workspaces` | 同步返回用户 Workspace |
| GET | `/workspaces/{workspace_id}/files` | 同步返回资源与统计 |
| POST | `/delete` | 幂等地异步删除完整文件；目标已不存在时任务仍成功 |
| POST | `/retrieval` | 同步返回混合检索结果 |
| GET | `/tasks/{task_id}?user_id=` | 任务状态、结果或错误 |
| GET | `/health/live` | 进程存活 |
| GET | `/health/ready` | 依赖就绪状态 |

`POST /add` 必须传 multipart 的 `user_id`、`workspace_id`、`workspace_name`、`type=file|str`。HTTP 文件使用 `file` 二进制字段，字符串使用 `content`。不接受路径或 JSON Base64。

新增和删除接口接受可选 `callback_url`（新增使用 multipart 表单字段，删除使用 JSON 字段）。回调携带 `task_id`、`status`、`stage`、`progress`，最终事件包含 `result` 或 `error`。不传时使用 `/tasks/{task_id}?user_id=` 轮询。Workspace 列表、资源列表和检索同步直返，不接受回调。

`user_id`、`workspace_id`、`workspace_name` 全部由前端提供，服务不自动生成。`workspace_id` 是最长 128 个字符的安全字符串标识符；`file_id` 为永久 `file_<UUID4>`；`task_id` 为 32 位小写十六进制字符串。每个检索结果都包含 UUID 格式的 `chunk_id`。哈希格式为 `sha256:` 加 SHA-256 的前 32 位小写十六进制字符。

统一错误：`{error:{code,message,request_id,details?}}`。完整示例见仓库 `docs/api.md`。
