#!/usr/bin/env bash
# 启动 Streamlit 面板（同事的浏览器打开这个）
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exec ./.venv/bin/streamlit run app.py
