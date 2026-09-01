#!/usr/bin/env bash
# 停止 MedRAG-Nexus：Next.js WebUI + API/MCP/Worker + Redis。
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
PID_DIR="$PROJECT_ROOT/.run"

if [ -f .env ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    case "$key" in
      ''|\#*) continue ;;
    esac
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

MANAGE_REDIS="${MANAGE_REDIS:-true}"

# 获取 Redis 端口（用于验证）
REDIS_PORT=$(echo "${REDIS_URL:-redis://127.0.0.1:20002/0}" | sed -n 's/.*:\/\/[^:]*:\([0-9]*\)\/.*/\1/p')
REDIS_PORT="${REDIS_PORT:-20002}"

APP_PORT="${APP_PORT:-28111}"
WEBUI_PORT="${WEBUI_PORT:-22134}"

c_ok()   { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[0;33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[0;31m%s\033[0m\n' "$*"; }
c_info() { printf '\033[0;36m%s\033[0m\n' "$*"; }

pid_from_file() {
  local file="$1" pid
  [ -f "$file" ] || return 1
  IFS= read -r pid < "$file" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] || return 1
  printf '%s' "$pid"
}

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

stop_pid_file() {
  local name="$1" file="$2" pid
  if [ ! -f "$file" ]; then
    c_warn "$name 未在运行（无 PID 文件）"
    return
  fi
  if ! pid="$(pid_from_file "$file")"; then
    c_warn "$name PID 文件无效（清理残留文件）"
    rm -f "$file"
    return
  fi
  if ! managed_pid_running "$pid"; then
    c_warn "$name 进程已不存在（清理残留 PID 文件）"
    rm -f "$file"
    return
  fi

  c_info "停止 $name (PID $pid) ..."
  if kill -0 -- "-$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  for _ in $(seq 1 10); do
    managed_pid_running "$pid" || break
    sleep 1
  done
  if managed_pid_running "$pid"; then
    c_warn "$name 未在 10s 内退出，发送 SIGKILL"
    if kill -0 -- "-$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$file"
  c_ok "$name 已停止"
}

# 先断开入口，再停止内含 Worker 和维护任务的后端。
stop_pid_file "WebUI" "$PID_DIR/webui.pid"
stop_pid_file "后端 API/MCP/Worker" "$PID_DIR/backend.pid"

if [ "$MANAGE_REDIS" = "true" ]; then
  if command -v docker >/dev/null 2>&1 && eval "$DOCKER_CMD compose version" >/dev/null 2>&1; then
    c_info "停止 Redis ..."
    eval "$DOCKER_CMD compose stop redis"
    c_ok "Redis 已停止（数据卷保留）"
  else
    c_warn "未找到可用的 docker compose，无法停止 Redis。"
  fi
else
  c_warn "MANAGE_REDIS=false，不停止外部 Redis。"
fi

echo
c_ok "全部已停止。数据和日志均已保留。"

# ---------- 停止后验证 ----------
echo
c_info "执行停止后验证..."

all_services_stopped=true

# 验证 Redis 是否已停止
if [ "$MANAGE_REDIS" = "true" ]; then
  if redis-cli -h 127.0.0.1 -p "$REDIS_PORT" ping 2>/dev/null | grep -q "PONG"; then
    c_err "✗ Redis 仍在运行 (127.0.0.1:$REDIS_PORT)"
    all_services_stopped=false
  else
    c_ok "✓ Redis 已停止"
  fi
fi

# 验证 API 是否已停止
if curl -q --noproxy '*' -fsS -m 2 "http://127.0.0.1:$APP_PORT/api/v1/health/live" >/dev/null 2>&1; then
  c_err "✗ API 仍在运行 (端口 $APP_PORT)"
  all_services_stopped=false
else
  c_ok "✓ API 已停止"
fi

# 验证 WebUI 是否已停止
if curl -q --noproxy '*' -fsS -m 2 "http://127.0.0.1:$WEBUI_PORT" >/dev/null 2>&1; then
  c_err "✗ WebUI 仍在运行 (端口 $WEBUI_PORT)"
  all_services_stopped=false
else
  c_ok "✓ WebUI 已停止"
fi

# 验证进程是否已停止
if [ -f "$PID_DIR/backend.pid" ]; then
  c_warn "后端 PID 文件仍存在"
  all_services_stopped=false
fi

if [ -f "$PID_DIR/webui.pid" ]; then
  c_warn "WebUI PID 文件仍存在"
  all_services_stopped=false
fi

# 检查端口是否释放
if netstat -tuln 2>/dev/null | grep -q ":$APP_PORT "; then
  c_err "✗ 端口 $APP_PORT 仍被占用"
  all_services_stopped=false
fi

if netstat -tuln 2>/dev/null | grep -q ":$WEBUI_PORT "; then
  c_err "✗ 端口 $WEBUI_PORT 仍被占用"
  all_services_stopped=false
fi

echo
if [ "$all_services_stopped" = true ]; then
  c_ok "所有服务停止验证通过！"
else
  c_err "部分服务仍占用资源，请检查进程和端口"
  exit 1
fi
