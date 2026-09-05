# 只打包网关和面板。Ollama 留在宿主机原生运行 —— macOS 上容器拿不到 Metal，
# 塞进来会退化成纯 CPU 推理。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OCR_HUB_DB=/data/hub.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py proxy.py app.py ./
COPY .streamlit ./.streamlit

# 数据库放命名卷里，不用宿主机 bind mount：
# macOS 的 VirtioFS 对 SQLite 的文件锁支持不可靠，而 WAL 模式重度依赖它。
VOLUME ["/data"]

EXPOSE 8000 8501
