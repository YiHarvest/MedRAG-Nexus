# JD Knowledge Console

JD Knowledge 的 Next.js Web 控制台，使用官方 Carbon Design System。页面直接调用 FastAPI 的真实接口，不包含演示数据。

## 功能

- 按用户加载并选择 Workspace，或在新增时输入新的 Workspace 名称。
- PDF、TXT、DOCX 文件或普通字符串异步入库。
- Workspace 文件列表与完整文件删除。
- Milvus + Elasticsearch + RRF + Rerank 混合检索。
- 异步任务查询与运行状态自动轮询。
- FastAPI 存活检查与全部基础设施就绪诊断。
- 用户 ID + Workspace 工作空间、深浅主题和移动端布局。

## 本地启动

先在仓库根目录启动 FastAPI：

```bash
uv run main.py --reload
```

再启动前端：

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

浏览器访问 `http://127.0.0.1:22134`。前端通过 Next.js rewrite 将 `/backend/*` 转发到 `API_BASE_URL`，当前本地默认是 `http://127.0.0.1:28111`，因此浏览器不需要额外配置 CORS。修改 `API_BASE_URL` 后必须重启 Next.js。

## 校验与生产运行

```bash
npm run lint
npm run typecheck
npm run build
npm run start
```

生产部署时在前端进程环境中设置：

```dotenv
API_BASE_URL=http://jd-knowledge-api:28111
```

`API_BASE_URL` 只在 Next.js 服务端代理中使用，不会作为公开浏览器环境变量暴露。
