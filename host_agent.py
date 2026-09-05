"""同时启动 Mac 原生指标服务和保留客户端 IP 的面板入口。"""

import asyncio
import os

import uvicorn

from metrics_agent import app as metrics_app
from panel_gateway import app as panel_gateway_app


async def _serve(app, host: str, port: int):
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    )
    server.install_signal_handlers = lambda: None
    await server.serve()


async def main():
    await asyncio.gather(
        _serve(
            metrics_app,
            os.environ.get("OCR_HUB_METRICS_BIND", "127.0.0.1"),
            int(os.environ.get("OCR_HUB_METRICS_PORT", "9105")),
        ),
        _serve(
            panel_gateway_app,
            os.environ.get("OCR_HUB_PANEL_BIND", "0.0.0.0"),
            int(os.environ.get("OCR_HUB_PANEL_PORT", "8501")),
        ),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
