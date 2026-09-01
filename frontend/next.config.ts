/**
 * Next.js 配置文件
 *
 * 该文件配置 Next.js 应用的构建和运行时行为。
 * 主要配置 API 请求的代理重写规则。
 */

import type { NextConfig } from "next";
import { resolve } from "node:path";
import { loadEnvFile } from "node:process";

// 前后端共享仓库根目录的 .env；已存在的进程环境变量仍有更高优先级。
loadEnvFile(resolve(process.cwd(), "../.env"));

// API 基础 URL，从环境变量读取，默认为本地开发地址
const apiBaseUrl = process.env.API_BASE_URL ?? "http://127.0.0.1:28111";

// 允许访问开发服务器资源的外部来源（逗号分隔），从环境变量读取
// 外部浏览器通过这些地址访问时，Next.js 默认会拦截开发资源（HMR、chunk 等）
const allowedDevOrigins = (process.env.ALLOWED_DEV_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

// Next.js 配置对象
const nextConfig: NextConfig = {
  allowedDevOrigins,
  experimental: {
    // 文件上传上限为 50 MiB；代理需要预留 multipart 表单开销。
    proxyClientMaxBodySize: "60mb",
  },

  /**
   * 配置 URL 重写规则
   *
   * 将前端 /backend/* 路径的请求代理到后端 API 服务。
   * 这样可以避免跨域问题，简化前端 API 调用。
   *
   * 示例：
   *   前端请求: /backend/api/v1/workspaces/workspace_xxx/files
   *   实际转发: http://127.0.0.1:28111/api/v1/workspaces/workspace_xxx/files
   */
  async rewrites() {
    return [
      {
        source: "/backend/:path*",  // 匹配 /backend/ 开头的所有路径
        destination: `${apiBaseUrl}/:path*`,  // 转发到后端 API
      },
    ];
  },
};

export default nextConfig;
