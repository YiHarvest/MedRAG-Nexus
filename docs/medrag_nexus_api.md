# MedRAG-Nexus Backend API

默认地址为 `http://127.0.0.1:28111`，交互文档位于 `/docs`。

## 路由边界

- `/api/v1/auth/*`：注册、登录、退出和当前账号。
- `/api/v1/account*`、`/api/v1/accounts*`、`/api/v1/permission-*`、`/api/v1/audit-events`：账号、密码、权限组和审计。
- `/api/v1/users*`、`/api/v1/workspaces*`：知识域、Workspace、ACL 和资源。
- `/api/v1/retrieval`、`/api/v1/chat/stream`、`/api/v1/tasks/*`：检索、聊天和任务。
- `/api/v1/agent/*`：Agent 动作、确认、输入和临时制品。
- `/api/v1/health/live`、`/api/v1/health/ready`：公开基础设施探针。

旧 `/api/webui/v1/*` 与原来的 `/api/v1/add`、`/api/v1/delete`、`/api/v1/delete-string` 等无鉴权重复入口不再提供。

## 认证与授权

`POST /api/v1/auth/register` 和 `POST /api/v1/auth/login` 成功后由后端设置 HttpOnly Session Cookie。除注册、登录和健康检查外，业务请求依次经过：

1. 可选部署外层门锁；
2. 后端账号 Session；
3. 权限节点；
4. 知识域和 Workspace ACL；
5. 服务端审计。

浏览器请求 `/backend/api/v1/*` 时，Next.js 会将它同源转发为后端 `/api/v1/*`。`/backend` 不是后端路由前缀。

## 后端创建资源

账号、知识域和 Workspace 均由 Python 后端完成校验与持久化：

- 注册账号时由后端生成 `account_id` 并创建 Session。
- 创建知识域时前端只需提交 `user_name` 等业务字段，后端默认生成 `user_id`。
- 创建 Workspace 时提交所属 `user_id` 和 `workspace_name`，后端生成 `workspace_id`。
- 前端的 API 模块只是请求封装，不直接写数据库，也不建立权限关系。

## 资源与异步任务

向 `POST /api/v1/workspaces/{workspace_id}/resources` 提交 multipart：

- 文件：`type=file` 和 `file`，支持 PDF、TXT、DOCX。
- 字符串：`type=str` 和 `content`。

接口从后端 Workspace 记录推导知识域和 Workspace 名称。新增、删除返回 `202` 与 `task_id`；使用 `GET /api/v1/tasks/{task_id}` 查询，使用 `DELETE /api/v1/tasks/{task_id}` 取消允许取消的任务。

## 健康检查

```text
GET /api/v1/health/live
GET /api/v1/health/ready
GET /api/v1/health/ready?details=true
```

健康检查是不建立账号 Session 的公开基础设施路由，可直接用于 Docker、Kubernetes 或反向代理探针。注册和登录也是匿名调用入口，但它们用于建立账号 Session，并仍可能受部署外层门锁保护。精确请求与响应模型以 `/openapi.json` 为准。
