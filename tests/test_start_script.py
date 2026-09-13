"""Run the startup script in isolation to verify local readiness probes."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def startup_project(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    shutil.copy2(PROJECT_ROOT / "scripts/start.sh", tmp_path / "scripts/start.sh")
    (tmp_path / "frontend/node_modules").mkdir(parents=True)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def executable(path: Path, body: str) -> None:
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o755)

    executable(
        bin_dir / "docker",
        'case "$*" in\n'
        '  "compose port redis 6379") echo "0.0.0.0:22002" ;;;\n'
        '  "compose exec -T redis redis-cli ping") echo PONG ;;;\n'
        "esac\n".replace(";;;", ";;"),
    )
    executable(bin_dir / "redis-cli", 'touch "$TEST_ROOT/cli-used"\nexit 127\n')
    executable(
        tmp_path / ".venv/bin/python",
        'echo "$REDIS_URL" >> "$TEST_ROOT/redis-probes"\nexit "$TEST_REDIS_STATUS"\n',
    )
    for command in ("uv", "npm"):
        executable(bin_dir / command, "exec sleep 60\n")
    executable(
        bin_dir / "curl",
        'url="${@: -1}"\necho "$url" >> "$TEST_ROOT/http-probes"\n'
        'case "$url" in\n'
        "  http://public.example:22134) exit 0 ;;;\n"
        '  http://127.0.0.1:28111/*) test -s "$TEST_ROOT/.run/backend.pid" ;;;\n'
        '  "$TEST_WEBUI_HEALTH") test -s "$TEST_ROOT/.run/webui.pid" ;;;\n'
        "  *) exit 7 ;;;\nesac\n".replace(";;;", ";;"),
    )
    (tmp_path / ".env").write_text(
        "APP_MODE=dev\nMANAGE_REDIS=true\nWEBUI_PUBLIC_ORIGIN=http://public.example:22134\n"
        "REDIS_URL=redis://127.0.0.1:20002/0\n"
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TEST_ROOT": str(tmp_path),
        "TEST_REDIS_STATUS": "0",
        "TEST_WEBUI_HEALTH": "http://127.0.0.1:22134",
        "REDIS_WAIT_SECONDS": "1",
        "API_WAIT_SECONDS": "3",
        "WEBUI_WAIT_SECONDS": "3",
    }
    yield tmp_path, env
    for pid_file in (tmp_path / ".run").glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass


def run_start(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/start.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_start_without_host_redis_cli_checks_local_webui(startup_project) -> None:
    root, env = startup_project
    result = run_start(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "所有服务启动验证通过" in result.stdout
    assert "WebUI:  http://public.example:22134" in result.stdout
    assert (root / ".run/webui.pid").exists()
    assert not (root / "cli-used").exists()
    assert set((root / "redis-probes").read_text().splitlines()) == {"redis://127.0.0.1:22002/0"}
    assert "http://public.example:22134" not in (root / "http-probes").read_text()


def test_container_pong_cannot_hide_failed_host_redis_connection(startup_project) -> None:
    root, env = startup_project
    result = run_start(root, env | {"TEST_REDIS_STATUS": "1"})

    assert result.returncode != 0
    assert "Redis 1s 内未就绪" in result.stdout
    assert not (root / ".run/backend.pid").exists()
    assert (root / "redis-probes").exists()


def test_webui_health_override_is_independent_of_display_url(startup_project) -> None:
    root, env = startup_project
    health = "http://127.0.0.1:23134/health"
    result = run_start(root, env | {"WEBUI_HEALTH_URL": health, "TEST_WEBUI_HEALTH": health})

    assert result.returncode == 0, result.stdout + result.stderr
    assert health in (root / "http-probes").read_text()
    assert "WebUI:  http://public.example:22134" in result.stdout
