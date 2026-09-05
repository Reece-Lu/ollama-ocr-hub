#!/usr/bin/env bash
# 配置 Ollama 的运行参数（macOS 桌面版需要用 launchctl 设置）
# 跑完这个脚本后，手动退出并重新打开 Ollama 应用才会生效
set -euo pipefail

launchctl setenv OLLAMA_KEEP_ALIVE "-1"
launchctl setenv OLLAMA_MAX_LOADED_MODELS "2"
launchctl setenv OLLAMA_NUM_PARALLEL "2"
launchctl setenv OLLAMA_CONTEXT_LENGTH "16384"
launchctl setenv OLLAMA_FLASH_ATTENTION "1"

# 故意不设置 OLLAMA_HOST：让 Ollama 只监听 127.0.0.1，
# 对外一律走网关，这样鉴权和限流才有意义。

echo "已写入。现在退出 Ollama 应用再重新打开。"
echo "然后拉模型： ollama pull deepseek-ocr"
