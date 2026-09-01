#!/usr/bin/env bash
# 一键安装 JD Knowledge 后端与 WebUI 的锁定版本依赖。
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# 加载根目录 .env；调用脚本前显式设置的环境变量优先。
if [ -f .env ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    case "$key" in
      ''|\#*) continue ;;
    esac
    if [[ "$value" == \"*\" && "$value" == *\" ]] || [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < .env
fi

APP_MODE="${APP_MODE:-dev}"

c_ok()   { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
c_info() { printf '\033[0;36m%s\033[0m\n' "$*"; }

case "$APP_MODE" in
  dev|prd) ;;
  *)
    c_err "APP_MODE 必须是 dev 或 prd，当前值：$APP_MODE"
    exit 1
    ;;
esac

command -v uv >/dev/null 2>&1 || {
  c_err "未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
}

# 与生产启动脚本保持一致：生产安装固定使用 /tmp 中的 Node.js 22。
if [ "$APP_MODE" = "prd" ]; then
  NODE22_PATH="/tmp/node-v22.20.0-linux-x64/bin"
  if [ ! -x "$NODE22_PATH/node" ] || [ ! -x "$NODE22_PATH/npm" ]; then
    c_err "生产模式需要 Node.js 22，但未找到 $NODE22_PATH/node 和 npm"
    exit 1
  fi
  export PATH="$NODE22_PATH:$PATH"
fi

for command_name in node npm; do
  command -v "$command_name" >/dev/null 2>&1 || {
    c_err "未找到必需命令：$command_name"
    exit 1
  }
done

NODE_VERSION="$(node --version)"
IFS='.' read -r NODE_MAJOR NODE_MINOR _ <<< "${NODE_VERSION#v}"
if ! [[ "$NODE_MAJOR" =~ ^[0-9]+$ && "$NODE_MINOR" =~ ^[0-9]+$ ]] \
  || [ "$NODE_MAJOR" -lt 20 ] \
  || { [ "$NODE_MAJOR" -eq 20 ] && [ "$NODE_MINOR" -lt 9 ]; }; then
  c_err "当前 Node.js 版本为 $NODE_VERSION，Next.js 16 需要 Node.js 20.9 或更高版本"
  exit 1
fi

if [ ! -f "$PROJECT_ROOT/uv.lock" ]; then
  c_err "缺少 uv.lock，无法保证 Python 依赖版本一致"
  exit 1
fi
if [ ! -f "$PROJECT_ROOT/frontend/package-lock.json" ]; then
  c_err "缺少 frontend/package-lock.json，无法保证 WebUI 依赖版本一致"
  exit 1
fi

if [ ! -f "$PROJECT_ROOT/.env" ]; then
  cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
  c_info "已由 .env.example 创建 .env，请在启动前填写密码和外部服务连接配置"
fi

c_info "安装锁定的 Python 后端与 WebUI BFF 依赖 ..."
uv sync --locked

c_info "安装锁定的 Next.js、React、Carbon WebUI 依赖 ..."
npm ci --prefix frontend

c_ok "依赖安装完成"
printf 'Python: %s\n' "$(uv run python --version)"
printf 'Node.js: %s\n' "$(node --version)"
printf 'npm: %s\n' "$(npm --version)"
printf '\n下一步：确认 .env 中的 APP_MODE 后执行 ./scripts/start.sh\n'
