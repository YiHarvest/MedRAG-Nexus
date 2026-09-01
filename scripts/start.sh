#!/usr/bin/env bash
# 一键启动 MedRAG-Nexus：Redis + API/MCP/Worker + Next.js WebUI。
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

# Docker 命令包装器：生产模式自动处理 sudo，开发模式不使用 sudo
DOCKER_CMD="docker"
if [ "$APP_MODE" = "prd" ]; then
  if ! docker ps >/dev/null 2>&1; then
    if [ -n "${SUDO_PASSWORD:-}" ]; then
      DOCKER_CMD="echo '$SUDO_PASSWORD' | sudo -S docker"
    else
      DOCKER_CMD="sudo docker"
    fi
  fi
fi
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-28111}"
WEBUI_HOST="${WEBUI_HOST:-0.0.0.0}"
WEBUI_PORT="${WEBUI_PORT:-22134}"
MANAGE_REDIS="${MANAGE_REDIS:-true}"
API_WAIT_SECONDS="${API_WAIT_SECONDS:-90}"
WEBUI_WAIT_SECONDS="${WEBUI_WAIT_SECONDS:-90}"
REDIS_WAIT_SECONDS="${REDIS_WAIT_SECONDS:-30}"
LOG_MAX_BYTES="${LOG_MAX_BYTES:-104857600}"
PID_DIR="$PROJECT_ROOT/.run"
mkdir -p "$PID_DIR"

API_ORIGIN="${API_BASE_URL:-http://127.0.0.1:${APP_PORT}}"
API_HEALTH_URL="${API_HEALTH_URL:-http://127.0.0.1:${APP_PORT}/api/v1/health/live}"
WEBUI_DEV_ORIGIN="${WEBUI_PUBLIC_ORIGIN:-http://127.0.0.1:${WEBUI_PORT}}"
WEBUI_URL="${WEBUI_URL:-$WEBUI_DEV_ORIGIN}"
WEBUI_ALLOWED_DEV_ORIGINS="${ALLOWED_DEV_ORIGINS:-127.0.0.1}"

c_ok()   { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[0;33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[0;31m%s\033[0m\n' "$*"; }
c_info() { printf '\033[0;36m%s\033[0m\n' "$*"; }

case "$APP_MODE" in
  dev|prd) ;;
  *)
    c_err "APP_MODE 必须是 dev 或 prd，当前值：$APP_MODE"
    exit 1
    ;;
esac

case "$MANAGE_REDIS" in
  true|false) ;;
  *)
    c_err "MANAGE_REDIS 必须是 true 或 false，当前值：$MANAGE_REDIS"
    exit 1
    ;;
esac

# 与素材库生产部署保持一致：构建和运行 WebUI 均使用 /tmp 中的 Node.js 22。
if [ "$APP_MODE" = "prd" ]; then
  NODE22_PATH="/tmp/node-v22.20.0-linux-x64/bin"
  if [ -x "$NODE22_PATH/node" ]; then
    export PATH="$NODE22_PATH:$PATH"
    c_info "生产模式：使用 Node.js $(node --version)"
  else
    c_err "生产模式需要 Node.js 22，但未找到 $NODE22_PATH/node"
    exit 1
  fi
fi

for command_name in uv npm curl setsid ps; do
  command -v "$command_name" >/dev/null 2>&1 || {
    c_err "未找到必需命令：$command_name"
    exit 1
  }
done

if [ ! -d .venv ]; then
  c_err "未找到 Python 虚拟环境。请先执行：./scripts/install.sh"
  exit 1
fi
if [ ! -d frontend/node_modules ]; then
  c_err "未找到 WebUI 依赖。请先执行：./scripts/install.sh"
  exit 1
fi

pid_from_file() {
  local file="$1" pid
  [ -f "$file" ] || return 1
  IFS= read -r pid < "$file" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] || return 1
  printf '%s' "$pid"
}

# 服务均通过 setsid 启动，PID 同时是进程组 ID；僵尸进程不算运行中。
managed_pid_running() {
  local pid="$1" process_pid process_group process_state
  while read -r process_pid process_group process_state; do
    if [ "$process_pid" = "$pid" ] || [ "$process_group" = "$pid" ]; then
      case "$process_state" in
        Z*) ;;
        *) return 0 ;;
      esac
    fi
  done < <(ps -eo pid=,pgid=,stat=)
  return 1
}

is_running() {
  local pid
  pid="$(pid_from_file "$1")" || return 1
  managed_pid_running "$pid"
}

stop_managed_process() {
  local file="$1" name="$2" pid
  pid="$(pid_from_file "$file")" || {
    rm -f "$file"
    return
  }
  if kill -0 -- "-$pid" 2>/dev/null; then
    c_info "停止异常的 $name 进程组 (PGID $pid) ..."
    kill -TERM -- "-$pid" 2>/dev/null || true
  elif kill -0 "$pid" 2>/dev/null; then
    c_info "停止异常的 $name 进程 (PID $pid) ..."
    kill -TERM "$pid" 2>/dev/null || true
  fi
  for _ in $(seq 1 10); do
    managed_pid_running "$pid" || break
    sleep 1
  done
  if managed_pid_running "$pid"; then
    if kill -0 -- "-$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$file"
}

rotate_log_if_needed() {
  local log_file="$1" name="$2" current_size rotated
  [ -f "$log_file" ] || return 0
  current_size="$(stat -c '%s' "$log_file" 2>/dev/null || printf '0')"
  [ "$current_size" -lt "$LOG_MAX_BYTES" ] || {
    rotated="$PID_DIR/${name}-$(date -u +%Y%m%dT%H%M%SZ).log"
    mv "$log_file" "$rotated"
    c_info "$(basename "$log_file") 已轮转为 $rotated"
  }
}

show_failure() {
  local message="$1" log_file="$2"
  c_err "$message"
  if [ -s "$log_file" ]; then
    c_err "日志末尾："
    tail -n 50 "$log_file" >&2 || true
  fi
  exit 1
}

# ---------- Redis ----------
EFFECTIVE_REDIS_URL="${REDIS_URL:-}"
if [ "$MANAGE_REDIS" = "true" ]; then
  command -v docker >/dev/null 2>&1 || {
    c_err "MANAGE_REDIS=true，但未找到 docker 命令。"
    exit 1
  }
  eval "$DOCKER_CMD compose version" >/dev/null 2>&1 || {
    c_err "当前 docker 不支持 compose 子命令。"
    exit 1
  }

  c_info "启动 Redis ..."
  eval "$DOCKER_CMD compose up -d redis"

  # 先尝试从 docker compose port 获取端口，失败则回退到从 .env 或 compose.yaml 解析
  redis_binding="$(eval "$DOCKER_CMD compose port redis 6379" 2>/dev/null || true)"
  redis_port="${redis_binding##*:}"
  
  if ! [[ "$redis_port" =~ ^[0-9]+$ ]]; then
    # 方法1：从 .env 中的 REDIS_URL 提取端口
    if [[ -n "${REDIS_URL:-}" ]]; then
      redis_port=$(echo "$REDIS_URL" | sed -n 's/.*:\/\/[^:]*:\([0-9]*\)\/.*/\1/p')
      if [[ "$redis_port" =~ ^[0-9]+$ ]]; then
        c_warn "无法从 Compose 获取端口，使用 .env 中的 REDIS_URL 端口: $redis_port"
      else
        # 方法2：从 compose.yaml 解析端口映射
        redis_port=$(grep -E '^\s*-\s*"[0-9]+:6379"' compose.yaml 2>/dev/null | sed 's/.*"\([0-9]*\):6379".*/\1/' | head -1 || true)
        if [[ "$redis_port" =~ ^[0-9]+$ ]]; then
          c_warn "从 compose.yaml 解析 Redis 端口: $redis_port"
        else
          c_err "无法获取 Redis 宿主机端口，请检查 compose.yaml 或在 .env 中配置 REDIS_URL"
          exit 1
        fi
      fi
    else
      # 从 compose.yaml 解析端口映射
      redis_port=$(grep -E '^\s*-\s*"[0-9]+:6379"' compose.yaml 2>/dev/null | sed 's/.*"\([0-9]*\):6379".*/\1/' | head -1 || true)
      if [[ "$redis_port" =~ ^[0-9]+$ ]]; then
        c_warn "从 compose.yaml 解析 Redis 端口: $redis_port"
      else
        c_err "无法获取 Redis 宿主机端口，请检查 compose.yaml 或在 .env 中配置 REDIS_URL"
        exit 1
      fi
    fi
  fi

  # 等待 Redis 真正就绪（从宿主机连接测试）
  redis_ready=false
  for _ in $(seq 1 "$REDIS_WAIT_SECONDS"); do
    # 同时检查容器内和宿主机连接
    if [ "$(eval "$DOCKER_CMD compose exec -T redis redis-cli ping" 2>/dev/null || true)" = "PONG" ] && \
       redis-cli -h 127.0.0.1 -p "$redis_port" ping 2>/dev/null | grep -q "PONG"; then
      redis_ready=true
      break
    fi
    sleep 1
  done
  if [ "$redis_ready" != true ]; then
    c_err "Redis ${REDIS_WAIT_SECONDS}s 内未就绪。"
    eval "$DOCKER_CMD compose logs --tail 50 redis" >&2 || true
    exit 1
  fi

  # 一键启动模式固定使用本 Compose Redis，避免 .env 中旧端口与 compose.yaml 不一致。
  EFFECTIVE_REDIS_URL="redis://127.0.0.1:${redis_port}/0"
  c_ok "Redis 就绪 ($EFFECTIVE_REDIS_URL)"
elif [ -z "$EFFECTIVE_REDIS_URL" ]; then
  c_err "MANAGE_REDIS=false 时必须配置 REDIS_URL。"
  exit 1
fi

# ---------- API + MCP + Worker ----------
BACKEND_PID_FILE="$PID_DIR/backend.pid"
BACKEND_LOG="$PID_DIR/backend.log"

api_is_ready() {
  curl -q --noproxy '*' -fsS -m 2 "$API_HEALTH_URL" >/dev/null 2>&1
}

if is_running "$BACKEND_PID_FILE" && ! api_is_ready; then
  c_warn "后端 PID 存在但健康检查失败，自动清理后重新启动。"
  stop_managed_process "$BACKEND_PID_FILE" "后端"
fi
if ! is_running "$BACKEND_PID_FILE" && api_is_ready; then
  c_err "后端监听地址已被未纳入 $BACKEND_PID_FILE 的服务占用。"
  c_err "请先停止旧实例，确认端口释放后再运行本脚本。"
  exit 1
fi

if is_running "$BACKEND_PID_FILE"; then
  c_warn "后端已在运行 (PID $(cat "$BACKEND_PID_FILE"))"
else
  rotate_log_if_needed "$BACKEND_LOG" backend
  c_info "启动 API + MCP + Worker [mode=$APP_MODE] ..."
  backend_args=(main.py)
  if [ "$APP_MODE" = "dev" ]; then
    backend_args+=(--reload)
  fi
  nohup setsid env REDIS_URL="$EFFECTIVE_REDIS_URL" \
    uv run "${backend_args[@]}" >> "$BACKEND_LOG" 2>&1 &
  backend_pid=$!
  echo "$backend_pid" > "$BACKEND_PID_FILE"

  backend_ready=false
  for _ in $(seq 1 "$API_WAIT_SECONDS"); do
    if api_is_ready; then
      c_ok "后端就绪 (PID $backend_pid)"
      backend_ready=true
      break
    fi
    if ! is_running "$BACKEND_PID_FILE"; then
      wait "$backend_pid" 2>/dev/null || backend_status=$?
      rm -f "$BACKEND_PID_FILE"
      show_failure "后端启动进程提前退出（状态码 ${backend_status:-0}）。" "$BACKEND_LOG"
    fi
    sleep 1
  done
  if [ "$backend_ready" != true ]; then
    stop_managed_process "$BACKEND_PID_FILE" "后端"
    show_failure "后端 ${API_WAIT_SECONDS}s 内未响应，已停止。" "$BACKEND_LOG"
  fi
fi

# ---------- Next.js WebUI ----------
WEBUI_PID_FILE="$PID_DIR/webui.pid"
WEBUI_LOG="$PID_DIR/webui.log"

webui_is_ready() {
  curl -q --noproxy '*' -fsS -m 2 "$WEBUI_URL" >/dev/null 2>&1
}

if is_running "$WEBUI_PID_FILE" && ! webui_is_ready; then
  c_warn "WebUI PID 存在但健康检查失败，自动清理后重新启动。"
  stop_managed_process "$WEBUI_PID_FILE" "WebUI"
fi
if ! is_running "$WEBUI_PID_FILE" && webui_is_ready; then
  c_err "WebUI 监听地址已被未纳入 $WEBUI_PID_FILE 的服务占用。"
  c_err "请先停止旧实例，确认端口释放后再运行本脚本。"
  exit 1
fi

if is_running "$WEBUI_PID_FILE"; then
  c_warn "WebUI 已在运行 (PID $(cat "$WEBUI_PID_FILE"))"
else
  rotate_log_if_needed "$WEBUI_LOG" webui
  if [ "$APP_MODE" = "prd" ]; then
    c_info "构建 WebUI [prd] ..."
    (
      cd frontend
      env API_BASE_URL="$API_ORIGIN" npm run build
    )
  fi

  c_info "启动 WebUI [mode=$APP_MODE] ..."
  pushd frontend >/dev/null
  if [ "$APP_MODE" = "dev" ]; then
    nohup setsid env API_BASE_URL="$API_ORIGIN" \
      WEBUI_PUBLIC_ORIGIN="$WEBUI_DEV_ORIGIN" \
      ALLOWED_DEV_ORIGINS="$WEBUI_ALLOWED_DEV_ORIGINS" \
      npm run dev -- --hostname "$WEBUI_HOST" --port "$WEBUI_PORT" \
      >> "$WEBUI_LOG" 2>&1 &
  else
    nohup setsid env API_BASE_URL="$API_ORIGIN" \
      npm run start -- --hostname "$WEBUI_HOST" --port "$WEBUI_PORT" \
      >> "$WEBUI_LOG" 2>&1 &
  fi
  webui_pid=$!
  popd >/dev/null
  echo "$webui_pid" > "$WEBUI_PID_FILE"

  webui_ready=false
  for _ in $(seq 1 "$WEBUI_WAIT_SECONDS"); do
    if webui_is_ready; then
      c_ok "WebUI 就绪 (PID $webui_pid)"
      webui_ready=true
      break
    fi
    if ! is_running "$WEBUI_PID_FILE"; then
      wait "$webui_pid" 2>/dev/null || webui_status=$?
      rm -f "$WEBUI_PID_FILE"
      show_failure "WebUI 启动进程提前退出（状态码 ${webui_status:-0}）。" "$WEBUI_LOG"
    fi
    sleep 1
  done
  if [ "$webui_ready" != true ]; then
    stop_managed_process "$WEBUI_PID_FILE" "WebUI"
    show_failure "WebUI ${WEBUI_WAIT_SECONDS}s 内未响应，已停止。" "$WEBUI_LOG"
  fi
fi

echo
c_ok "MedRAG-Nexus 全部服务已就绪。 [mode=$APP_MODE]"
echo "  WebUI:  $WEBUI_URL"
echo "  API:    $API_ORIGIN"
echo "  Swagger: ${API_ORIGIN%/}/docs"
echo "  MCP:     ${API_ORIGIN%/}/mcp"
echo "  日志:    $PID_DIR/{backend,webui}.log"
echo "  关闭:    ./scripts/stop.sh"

# ---------- 启动后验证 ----------
echo
c_info "执行启动后验证..."

all_services_ok=true

# 验证 Redis
if [ "$MANAGE_REDIS" = "true" ]; then
  if redis-cli -h 127.0.0.1 -p "$redis_port" ping 2>/dev/null | grep -q "PONG"; then
    c_ok "✓ Redis 验证通过 (127.0.0.1:$redis_port)"
  else
    c_err "✗ Redis 验证失败"
    all_services_ok=false
  fi
fi

# 验证 API
if curl -q --noproxy '*' -fsS -m 5 "$API_HEALTH_URL" >/dev/null 2>&1; then
  c_ok "✓ API 验证通过 ($API_ORIGIN)"
else
  c_err "✗ API 验证失败"
  all_services_ok=false
fi

# 验证 WebUI
if curl -q --noproxy '*' -fsS -m 5 "$WEBUI_URL" >/dev/null 2>&1; then
  c_ok "✓ WebUI 验证通过 ($WEBUI_URL)"
else
  c_err "✗ WebUI 验证失败"
  all_services_ok=false
fi

# 验证进程
if [ -f "$BACKEND_PID_FILE" ]; then
  backend_pid=$(cat "$BACKEND_PID_FILE")
  if kill -0 "$backend_pid" 2>/dev/null; then
    c_ok "✓ 后端进程运行中 (PID $backend_pid)"
  else
    c_err "✗ 后端进程已退出"
    all_services_ok=false
  fi
fi

if [ -f "$WEBUI_PID_FILE" ]; then
  webui_pid=$(cat "$WEBUI_PID_FILE")
  if kill -0 "$webui_pid" 2>/dev/null; then
    c_ok "✓ WebUI 进程运行中 (PID $webui_pid)"
  else
    c_err "✗ WebUI 进程已退出"
    all_services_ok=false
  fi
fi

echo
if [ "$all_services_ok" = true ]; then
  c_ok "所有服务启动验证通过！"
else
  c_err "部分服务启动验证失败，请检查日志："
  echo "  - 后端日志: $BACKEND_LOG"
  echo "  - WebUI 日志: $WEBUI_LOG"
  exit 1
fi
