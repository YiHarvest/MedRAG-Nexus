<div align="center">

  <h1>MedRAG-Nexus</h1>

  <p align="center">面向 AgentHub 与自有 WebUI 的多用户、多 Workspace 异步知识入库、权限治理和混合检索服务。</p>

  <p align="center"><a href="https://github.com/YiHarvest/MedRAG-Nexus">GitHub Repository</a></p>

  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/FastAPI-0.116%2B-009688?logo=fastapi&logoColor=white" alt="FastAPI">
    <img src="https://img.shields.io/badge/FastMCP-3.x-7C3AED" alt="FastMCP 3">
    <img src="https://img.shields.io/badge/Next.js-16-black?logo=next.js" alt="Next.js 16">
    <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white" alt="Redis 7">
  </p>

</div>

## Choose Your Path

<div align="center">
  <table>
    <tr>
      <td colspan="3" align="center">
        <a href="#architecture---双-runtime-隔离架构">
          <img src="docs/assets/medrag-nexus-runtime.svg" alt="MedRAG-Nexus Runtime Architecture" width="900">
        </a>
      </td>
    </tr>
    <tr>
      <td align="center">
        <strong>使用知识库？</strong><br>
        从 <a href="#webui---账号权限与知识助手">WebUI</a> 开始
      </td>
      <td align="center">
        <strong>接入 AgentHub？</strong><br>
        查看 <a href="#api--mcp---公开集成接口">API &amp; MCP</a>
      </td>
      <td align="center">
        <strong>部署服务？</strong><br>
        直接执行 <a href="#quick-start---一键安装启动与停止">Quick Start</a>
      </td>
    </tr>
  </table>
  <p>
    <strong>首次安装？</strong> 使用锁定依赖的一键脚本 ·
    <strong>已有外部 Redis？</strong> 使用 <code>MANAGE_REDIS=false</code> ·
    <strong>准备上线？</strong> 在 <code>.env</code> 中设置 <code>APP_MODE=prd</code>
  </p>
</div>

---

## Quick Start - 一键安装、启动与停止

一键脚本负责安装后端与前端依赖、管理本地 Redis、启动后端和 WebUI，并在返回成功前执行健康检查。Elasticsearch、Milvus 和模型服务需要预先配置并可访问。

### 1. 准备运行环境

| 依赖 | 最低要求 | 用途 |
| --- | --- | --- |
| Python | 3.10+ | FastAPI、FastMCP、Worker 与知识处理 |
| [uv](https://docs.astral.sh/uv/) | 可用的当前版本 | 按根目录 `uv.lock` 安装 Python 锁定依赖 |
| Node.js | 20.9+ | Next.js 16 WebUI；生产脚本固定使用 Node.js 22 |
| npm | 与 Node.js 配套 | 按 `frontend/package-lock.json` 安装前端锁定依赖 |
| Docker Compose | `docker compose` 可用 | 默认启动 Compose Redis |
| redis-cli、curl、setsid、ps | 系统命令可用 | 启停脚本的就绪检查与进程管理 |

生产模式要求下列文件存在：

```text
/tmp/node-v22.20.0-linux-x64/bin/node
/tmp/node-v22.20.0-linux-x64/bin/npm
```

### 2. 安装锁定依赖

**开发环境**

```bash
./scripts/install.sh
```

**生产环境**

```bash
./scripts/install.sh
```

安装脚本会读取 `.env` 中的 `APP_MODE`，再执行 `uv sync --locked` 与 `npm ci --prefix frontend`。如果根目录不存在 `.env`，脚本会从 `.env.example` 创建；锁文件属于项目版本的一部分，不要手工删除或编辑。

### 3. 配置最小可用环境

在根目录 `.env` 中至少确认以下配置。请使用真实强密码和实际服务地址，不要提交 `.env`：

```dotenv
# 运行模式：dev 或 prd；安装、启动和停止脚本都会读取此值
APP_MODE=prd
DEV_DATABASE_NAME=MedRAG-Nexus-dev
PRD_DATABASE_NAME=MedRAG-Nexus-prd

# 首次启动且账号表为空时创建 WebUI 超级管理员
WEBUI_SUPERADMIN_USERNAME=admin
WEBUI_SUPERADMIN_PASSWORD=replace-with-a-strong-password
WEBUI_SUPERADMIN_DISPLAY_NAME=超级管理员

# 必需的检索存储
ELASTICSEARCH_URL=http://127.0.0.1:9200
ELASTICSEARCH_USERNAME=elastic
ELASTICSEARCH_PASSWORD=replace-me
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
MILVUS_TOKEN=

# 必需的 OpenAI-compatible Embedding 服务
OPENAI_EMBEDDING_URL=http://127.0.0.1:11434/v1/embeddings
OPENAI_EMBEDDING_API_KEY=
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIMENSION=1024

# 使用 WebUI 知识助手时配置
LLM_URL=http://127.0.0.1:11434/v1
LLM_MODEL=your-chat-model
LLM_KEY=
```

`WEBUI_SUPERADMIN_PASSWORD` 是真实账号密码；`WEBUI_LOCK_PASSWORD` 只是可选的部署外层门锁，两者不要复用。需要一次初始化多个超级管理员时，使用优先级更高的 `WEBUI_SUPERADMINS_JSON`：

```dotenv
WEBUI_SUPERADMINS_JSON='[{"login_name":"admin-a","display_name":"管理员 A","password":"replace-me"},{"login_name":"admin-b","display_name":"管理员 B","password":"replace-me"}]'
```

### 4. 一键启动

**开发模式**

```bash
./scripts/start.sh
```

**生产模式**

```bash
./scripts/start.sh
```

启动脚本按顺序完成：

1. 启动并验证 Compose Redis；
2. 启动 FastAPI、FastMCP、MCP/Backend Runtime、Redis Worker 与维护任务；
3. 开发模式启动 Next.js Dev Server，生产模式先构建再启动 Next.js；
4. 验证 Redis、API、WebUI 和托管进程，任一失败都会返回非零状态并显示日志位置。

### 5. 一键停止

```bash
./scripts/stop.sh
```

安装、启动和停止脚本都会读取 `.env` 中的 `APP_MODE`；调用脚本前显式设置的同名环境变量仍具有更高优先级。停止脚本先关闭 WebUI，再关闭内含 Worker 和维护任务的后端，最后停止 Compose Redis。Redis 数据卷、`data/` 业务数据和日志都会保留。

### 服务地址

| 服务 | 默认地址 | 说明 |
| --- | --- | --- |
| WebUI | [http://127.0.0.1:22134](http://127.0.0.1:22134) | Next.js 控制台 |
| API | [http://127.0.0.1:28111](http://127.0.0.1:28111) | FastAPI 服务 |
| Swagger UI | [http://127.0.0.1:28111/docs](http://127.0.0.1:28111/docs) | HTTP 接口文档 |
| FastMCP | `http://127.0.0.1:28111/mcp` | Streamable HTTP |
| Live Health | `http://127.0.0.1:28111/api/v1/health/live` | 进程存活检查 |
| Ready Health | `http://127.0.0.1:28111/api/v1/health/ready` | 外部依赖就绪检查 |

**[完整环境变量](.env.example)** | **[启动脚本](scripts/start.sh)** | **[停止脚本](scripts/stop.sh)**

---

## Dependencies - 外部服务与配置

`uv sync` 和 `npm ci` 只安装代码依赖，不会安装 Elasticsearch、Milvus 或模型服务。

| 服务 | 是否必需 | 一键脚本是否管理 | 关键变量 |
| --- | --- | --- | --- |
| Redis 7 | 必需 | 默认管理 | `MANAGE_REDIS`、`REDIS_URL`；队列名由 `APP_MODE` 派生 |
| Elasticsearch 8.x | 必需 | 否 | `ELASTICSEARCH_URL`、认证信息；索引名由 `APP_MODE` 派生 |
| Milvus 2.5+ | 必需 | 否 | `MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_TOKEN`；集合名由 `APP_MODE` 派生 |
| OpenAI-compatible Embedding | 必需 | 否 | `OPENAI_EMBEDDING_URL`、Key、模型、维度 |
| OpenAI-compatible LLM | WebUI 聊天必需 | 否 | `LLM_URL`、`LLM_MODEL`、`LLM_KEY` |
| MinerU 远端服务 | 可选；Python 包已由 uv 安装 | 否 | `MINERU_URL`、`MINERU_BACKEND`、并发与超时 |
| Rerank | 精排可选 | 否 | `RERANK_URL`、`RERANK_MODEL`、`RERANK_API_KEY` |

### Redis：内置 Compose 或外部实例

默认 `MANAGE_REDIS=true`。Compose 将容器 `6379` 映射到宿主机 `22002`；启动脚本会动态读取实际映射，并把正确的 `REDIS_URL` 传给后端，因此不依赖 `.env` 中的旧端口值。

使用外部 Redis 时：

```bash
MANAGE_REDIS=false \
REDIS_URL=redis://redis.example.internal:6379/0 \
./scripts/start.sh
```

停止时保留外部 Redis：

```bash
MANAGE_REDIS=false ./scripts/stop.sh
```

### 配置分组

| 配置域 | 关键变量 | 说明 |
| --- | --- | --- |
| 应用监听 | `APP_HOST`、`APP_PORT`、`APP_LOG_LEVEL` | 后端默认监听 `0.0.0.0:28111` |
| WebUI 监听 | `WEBUI_HOST`、`WEBUI_PORT`、`API_BASE_URL` | 启动脚本默认 WebUI 为 `0.0.0.0:22134` |
| WebUI 安全 | `WEBUI_COOKIE_SECURE`、`WEBUI_LOCK_PASSWORD`、`WEBUI_TRUST_PROXY_HEADERS` | 反向代理后才开启可信代理头，并正确设置 hops |
| MCP 数据 | `DATA_ROOT`、`SQLITE_PATH`、`ELASTICSEARCH_*_INDEX`、`MILVUS_COLLECTION` | MCP 集成使用 |
| Backend 数据 | `WEBUI_DATA_ROOT`、`WEBUI_SQLITE_PATH`、`WEBUI_ELASTICSEARCH_*_INDEX`、`WEBUI_MILVUS_COLLECTION` | 与 MCP 数据隔离 |
| 任务控制 | `WORKER_CONCURRENCY`、`FILE_INGESTION_CONCURRENCY`、`TASK_TIMEOUT_SECONDS`、`STAGE_RETRY_COUNT` | 控制队列消费、重试与资源压力 |
| 检索参数 | `CHUNK_SIZE`、`CHUNK_OVERLAP`、`RETRIEVAL_*`、`RRF_K` | 控制切块、候选召回与融合 |
| 数据限制 | `MAX_FILE_SIZE_MIB`、`MAX_TEXT_SIZE_MIB` | 默认文件 50 MiB、文本 10 MiB |

Backend Runtime 默认复用 MCP Runtime 的 Elasticsearch、Milvus、Redis 连接地址和认证，只隔离 SQLite、文件目录、索引、Collection 与任务队列。需要单独基础设施时可配置 `WEBUI_ELASTICSEARCH_*`、`WEBUI_MILVUS_*` 和 `WEBUI_REDIS_URL`。

脚本加载优先级为：调用命令前显式设置的环境变量 > 根目录 `.env` > 代码默认值。

### 手动开发启动

需要拆分调试时：

```bash
# 终端 1：基础 Redis
docker compose up -d redis

# 终端 2：API + MCP + 两套 Runtime + Worker
REDIS_URL=redis://127.0.0.1:22002/0 uv run main.py --reload

# 终端 3：Next.js WebUI
API_BASE_URL=http://127.0.0.1:28111 npm --prefix frontend run dev
```

手动停止：

```bash
docker compose stop redis
```

---

## WebUI - 账号、权限与知识助手

WebUI 使用 Next.js 16 与 Carbon Design System。浏览器只提交账号 Session；知识身份、Workspace 权限和 Agent 工具能力由服务端 BFF 决定并再次校验。

- 支持 PDF、TXT、DOCX 拖拽上传与普通文本入库，并展示异步任务进度。
- 支持知识域、Workspace、文件、字符串、检索与聊天。
- 成员等级为 `0 / 1 / 2 / 1000`，自定义权限组的权限节点取并集。
- UserID 与 Workspace ACL 默认拒绝，显式 `deny` 优先；Workspace 还必须通过父 UserID 权限链。
- 超级管理员、知识域负责人和 Workspace 创建者的系统 ACL 明确写库并可审计。
- Agent 根据当前账号、权限组和资源 ACL 动态获得工具；高风险操作要求二次确认。
- 账号、Session、权限、知识资源和 Agent Action 写入 SQLite 审计表，并可靠导出到按日 JSONL。
- 聊天正文只存浏览器；服务端仅保留 Action、结果摘要和有时效的临时制品元数据。

`WEBUI_LOCK_PASSWORD` 保护 WebUI 和 `/api/v1/*` 业务接口；`/api/v1/health/*` 保持公开供基础设施探测。`/mcp` 是独立集成入口。默认不信任 `X-Forwarded-For`；只有受控反向代理会覆盖或追加代理头时，才设置：

```dotenv
WEBUI_TRUST_PROXY_HEADERS=true
WEBUI_TRUSTED_PROXY_HOPS=1
WEBUI_COOKIE_SECURE=true
```

**[权限插件规范](docs/webui_permission_plugins.md)** | **[AgentHub Skill](skills/medrag-nexus/SKILL.md)**

---

## Backend API 与 MCP

REST 业务接口统一使用 `/api/v1/*`，由后端账号 Session、权限节点和资源 ACL 保护。浏览器通过 Next.js 的 `/backend/api/v1/*` 同源代理访问同一组接口，不再存在 `/api/webui/v1/*` 或另一套无鉴权业务路由。

| Surface | Path / Tool | 行为 |
| --- | --- | --- |
| HTTP | `/api/v1/auth/*`、`/api/v1/account*` | 后端注册、登录、Session、账号与密码管理 |
| HTTP | `/api/v1/users*`、`/api/v1/workspaces*` | 后端创建知识域/知识库并执行权限与 ACL 校验 |
| HTTP | `POST /api/v1/workspaces/{id}/resources` | 新增 PDF、TXT、DOCX 或字符串，异步返回任务 |
| HTTP | `POST /api/v1/retrieval`、`POST /api/v1/chat/stream` | 混合检索与流式聊天 |
| HTTP | `/api/v1/tasks/{task_id}` | 查询或取消异步任务 |
| HTTP | `/api/v1/agent/*` | Agent 动作、确认和临时制品 |
| HTTP | `/api/v1/health/live`、`/api/v1/health/ready` | 无需账号身份的公开存活与就绪检查 |
| MCP | `add` | 新增文件或字符串 |
| MCP | `list_workspaces`、`list_files` | 查询 Workspace 和资源 |
| MCP | `delete_file` | 删除完整资源 |
| MCP | `get_task` | 查询异步任务 |
| MCP | `retrieve` | 混合检索 |

前端创建账号、知识域和知识库时只提交业务字段；账号 ID、默认知识域 ID 和 Workspace ID 由后端生成并持久化。新增和删除通过任务接口查询状态。文件和字符串分别按 SHA-256 去重；文件 ID 创建后保持稳定。

> [!WARNING]
> `/mcp` 继续服务受信任的 AgentHub 集成，授权边界独立于 REST Session。不要把 MCP 入口直接暴露到不可信网络。

**[HTTP API 文档](docs/medrag_nexus_api.md)** | **[MCP 工具契约](skills/medrag-nexus/references/mcp-tools.md)** | **[Skill API Contract](skills/medrag-nexus/references/api-contract.md)**

---

## Architecture - 双 Runtime 隔离架构

一个 FastAPI 进程同时承载统一 Backend API、FastMCP、两套 Runtime、Worker 和维护任务：

| Runtime | 调用入口 | SQLite / 文件 | Elasticsearch / Milvus | Redis Queue |
| --- | --- | --- | --- | --- |
| MCP Runtime | `/mcp` | `SQLITE_PATH`、`DATA_ROOT` | `ELASTICSEARCH_*_INDEX`、`MILVUS_COLLECTION` | `knowledge:tasks` |
| Backend Runtime | `/api/v1/*`（Next.js 经 `/backend/api/v1/*` 代理） | `WEBUI_SQLITE_PATH`、`WEBUI_DATA_ROOT` | `WEBUI_ELASTICSEARCH_*_INDEX`、`WEBUI_MILVUS_COLLECTION` | `knowledge:webui:tasks` |

两套 Runtime 可以复用相同的 Redis、Elasticsearch、Milvus 和模型服务连接，但命名空间与本地权威数据完全分离，避免账号业务数据和外部 AgentHub 调用方混用。

上方 SVG 架构图使用 Archify 从本仓库运行时事实生成，并通过 showcase 级结构、连线、标签与桌面可读性校验。

---

## Data Flow - 入库、检索与删除

**新增**

```text
临时写入并解析
→ 写 Elasticsearch / Milvus（失败阶段最多重试 3 次）
→ 原子发布正式文件
→ 同一 SQLite 事务提交资源元数据、统计与任务 succeeded/100%
→ 资源对列表可见
```

**检索**

```text
用户问题
→ Elasticsearch BM25 + Milvus Vector
→ RRF 融合
→ 可选 Rerank
→ 返回带来源信息的命中
```

**删除**

```text
移入 Workspace 回收区
→ 清理 Elasticsearch / Milvus
→ 提交 SQLite 删除与统计
→ 清理回收区
→ succeeded
```

任何阶段失败都会记录任务错误并执行补偿；补偿仍失败时会阻止 Workspace 继续写入，等待恢复或维护任务接管。

---

## Storage - 数据布局

```text
./data/
└── MedRAG-Nexus-{dev|prd}/
    ├── MedRAG-Nexus-{dev|prd}.sqlite3
    ├── workspaces/{sha256(user_id)}/{workspace_id}/
    ├── v3_staging/{task_id}/
    ├── v3_recycle/{task_id}/
    ├── webui/
    │   ├── MedRAG-Nexus-{dev|prd}-webui.sqlite3
    │   └── workspaces/{sha256(user_id)}/{workspace_id}/
    └── audit/webui/webui-audit-YYYY-MM-DD.jsonl
```

`APP_MODE=dev` 与 `APP_MODE=prd` 使用完全不同的 SQLite 文件、Elasticsearch 索引、Milvus Collection、Redis 队列和文件目录。Elasticsearch 使用 `medrag-nexus-{mode}-*`，Milvus 使用后端允许的 `medrag_nexus_{mode}_*`。

---

## Observability - 进程与业务日志

| 路径 | 内容 |
| --- | --- |
| `.run/backend.log` | 一键脚本托管的后端 stdout / stderr |
| `.run/webui.log` | 一键脚本托管的 Next.js stdout / stderr |
| `data/MedRAG-Nexus-{mode}/log/api/YYYY-MM-DD.log` | HTTP / MCP 请求、状态码、大小、耗时和 Worker 生命周期 |
| `data/MedRAG-Nexus-{mode}/log/retrieval/YYYY-MM-DD.log` | BM25、向量召回、RRF、Rerank 与总耗时 |
| `data/MedRAG-Nexus-{mode}/log/tasks/{task_id}.log` | 单个异步任务的解析、Embedding、索引、重试和补偿阶段 |
| `data/MedRAG-Nexus-{mode}/audit/webui/webui-audit-YYYY-MM-DD.jsonl` | WebUI 身份、请求、资源操作与权限变更 |

API Key、密码、Cookie、上传正文和检索命中文本不会写入日志。业务日志默认保留 7 天；WebUI 审计默认保留 3 个自然月。

---

## Packages

| Package / Directory | Description |
| --- | --- |
| `src/medrag_nexus/api` | FastAPI 装配、公开路由、文档和 HTTP 基础设施 |
| `src/medrag_nexus/mcp` | FastMCP Streamable HTTP 与 AgentHub 工具 |
| `src/medrag_nexus/services` | Runtime、Worker、任务、检索、处理、回调和维护 |
| `src/medrag_nexus/storage` | SQLite、Redis、Elasticsearch、Milvus 与文件制品 |
| `src/medrag_nexus/pipeline` | 解析、Markdown、切块与融合模型 |
| `src/medrag_nexus/webui` | 账号、Session、权限、ACL、审计和 Agent BFF |
| `frontend` | Next.js 16 + React 19 + Carbon Design System |
| `skills/medrag-nexus` | AgentHub Skill 与 API/MCP 契约 |
| `scripts` | 锁定安装、一键启动和一键停止 |

## Resources

- [完整环境变量](.env.example) — 所有运行时配置和默认值
- [HTTP API 文档](docs/medrag_nexus_api.md) — API 用法与请求响应
- [WebUI 权限插件规范](docs/webui_permission_plugins.md) — 外部权限插件扩展点
- [AgentHub Skill](skills/medrag-nexus/SKILL.md) — Agent 使用说明
- [系统架构 SVG](docs/assets/medrag-nexus-runtime.svg) — 支持深浅色显示的可编辑矢量图

## Development

```bash
# Python lint 与测试
uv run ruff check src tests
uv run pytest -q

# WebUI 类型检查与生产构建
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

使用 Podman Docker API socket 的生产服务器：

```bash
export DOCKER_HOST=unix:///run/podman/podman.sock
cd /root/MedRAG-Nexus
git pull --ff-only origin main
./scripts/start.sh
```

停止：

```bash
export DOCKER_HOST=unix:///run/podman/podman.sock
cd /root/medrag_nexus
./scripts/stop.sh
```
