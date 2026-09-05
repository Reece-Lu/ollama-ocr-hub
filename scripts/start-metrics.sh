#!/usr/bin/env bash
# 在 macOS 宿主机原生启动资源指标服务和面板入口（不要放进 Docker）
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exec ./.venv/bin/python host_agent.py
