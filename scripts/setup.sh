#!/usr/bin/env bash
# 一次性初始化：建虚拟环境、装依赖、建数据库
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python db.py

[ -f .env ] || cp .env.example .env

echo
echo "装好了。接下来："
echo "  1. 编辑 .env，把 OCR_HUB_HOST 改成这台机器的局域网 IP"
echo "  2. 跑一次 scripts/ollama-env.sh 配置 Ollama，然后重启 Ollama"
echo "  3. 两个终端分别跑 scripts/start-proxy.sh 和 scripts/start-panel.sh"
