#!/usr/bin/env bash
# 启动鉴权网关（同事的程序调这个）
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exec ./.venv/bin/python proxy.py
