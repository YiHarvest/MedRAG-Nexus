# Backend HTTP 接口摘要

基础地址默认 `http://127.0.0.1:28111`。所有 REST 业务路径统一以 `/api/v1` 开头；旧 `/api/webui/v1` 和原无鉴权业务接口已移除。

除注册、登录和健康检查外，业务接口都要求后端账号 Session，并继续校验权限节点、知识域和 Workspace ACL。浏览器使用 `/backend/api/v1/*` 同源代理；该前缀只存在于 Next.js，转发到后端时仍是 `/api/v1/*`。

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 后端注册账号、生成账号 ID、创建 Session |
| POST | `/api/v1/auth/login` | 登录并创建 Session |
| GET | `/api/v1/auth/me` | 返回当前账号和权限 |
| POST | `/api/v1/users` | 注册知识域；`user_id` 只能由后端生成 |
| POST | `/api/v1/workspaces` | 创建 Workspace，`workspace_id` 由后端生成 |
| GET | `/api/v1/workspaces` | 按 Session、权限和 ACL 返回可见知识域与 Workspace |
| POST | `/api/v1/workspaces/{workspace_id}/resources` | multipart 新增文件或字符串，返回 `202 {task_id}` |
| DELETE | `/api/v1/workspaces/{workspace_id}/files/{file_id}` | 异步删除文件 |
| DELETE | `/api/v1/workspaces/{workspace_id}/strings/{content_hash}` | 异步删除字符串 |
| POST | `/api/v1/retrieval` | 在有读取权限的 Workspace 中检索 |
| POST | `/api/v1/chat/stream` | 基于可见知识流式聊天 |
| GET/DELETE | `/api/v1/tasks/{task_id}` | 查询或取消异步任务 |
| GET | `/api/v1/health/live`、`/api/v1/health/ready` | 公开基础设施探针 |

资源新增使用 multipart：`type=file` 时传二进制 `file`；`type=str` 时传 `content`。`user_id` 从目标 Workspace 的后端记录推导，不接受前端用请求体越权指定。

账号、知识域和 Workspace 注册都由 Python 后端校验、生成标识并持久化；前端模块只是调用封装。REST 知识域注册不接受 `user_id`，Workspace 注册不接受 `workspace_id`。MCP 保留独立工具契约，不能把 MCP 的调用方 ID 规则套用到 REST 业务接口。

受保护路由的认证错误使用 `{detail:{code,message}}`；通用基础设施错误使用 `{error:{code,message,request_id,details?}}`。完整路由和模型以运行时 `/docs` 与 `/openapi.json` 为准。
