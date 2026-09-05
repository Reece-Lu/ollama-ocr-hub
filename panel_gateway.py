"""macOS 宿主机上的面板入口。

Docker Desktop 发布端口时会代替浏览器连接容器，容器里看到的来源地址通常是
192.168.65.1。这个很薄的反向代理原生运行在 Mac 上，先取得真实 TCP 来源 IP，
再通过 X-Forwarded-For 交给只绑定在 127.0.0.1:8502 的 Streamlit 容器。
"""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

UPSTREAM_HTTP = os.environ.get(
    "OCR_HUB_PANEL_UPSTREAM", "http://127.0.0.1:8502"
).rstrip("/")
UPSTREAM_WS = UPSTREAM_HTTP.replace("http://", "ws://", 1).replace(
    "https://", "wss://", 1
)

client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False)
    yield
    await client.aclose()


app = FastAPI(
    title="OCR Hub Panel Gateway", docs_url=None, redoc_url=None, lifespan=lifespan
)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _client_ip(connection: Request | WebSocket) -> str:
    return connection.client.host if connection.client else ""


def _forward_headers(connection: Request | WebSocket) -> dict[str, str]:
    headers = {
        name: value
        for name, value in connection.headers.items()
        if name.lower() not in _HOP_BY_HOP
    }
    source_ip = _client_ip(connection)
    # 入口直接覆盖而不是追加客户端传来的值，防止伪造登记 IP。
    headers["x-forwarded-for"] = source_ip
    headers["x-real-ip"] = source_ip
    headers["x-forwarded-proto"] = "https" if connection.url.scheme == "https" else "http"
    headers["x-forwarded-host"] = connection.headers.get("host", "")
    return headers


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def forward_http(request: Request, path: str):
    url = f"{UPSTREAM_HTTP}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    upstream = await client.request(
        request.method,
        url,
        headers=_forward_headers(request),
        content=await request.body(),
    )

    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name, value in upstream.headers.multi_items():
        lower = name.lower()
        if lower in _HOP_BY_HOP or lower in {"content-length", "content-encoding"}:
            continue
        if lower == "content-type":
            response.headers[name] = value
        else:
            response.headers.append(name, value)
    return response


@app.websocket("/{path:path}")
async def forward_websocket(websocket: WebSocket, path: str):
    url = f"{UPSTREAM_WS}/{path}"
    if websocket.url.query:
        url += f"?{websocket.url.query}"

    headers = _forward_headers(websocket)
    for name in list(headers):
        if name.lower() in {"host", "origin"} or name.lower().startswith("sec-websocket-"):
            headers.pop(name)

    try:
        async with websocket_connect(
            url,
            additional_headers=headers,
            subprotocols=websocket.scope.get("subprotocols") or None,
            compression=None,
            max_size=None,
            proxy=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def browser_to_panel():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close()
                        return
                    payload = message.get("bytes")
                    if payload is None:
                        payload = message.get("text", "")
                    await upstream.send(payload)

            async def panel_to_browser():
                async for payload in upstream:
                    if isinstance(payload, bytes):
                        await websocket.send_bytes(payload)
                    else:
                        await websocket.send_text(payload)

            tasks = [
                asyncio.create_task(browser_to_panel()),
                asyncio.create_task(panel_to_browser()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except (WebSocketDisconnect, ConnectionClosed):
        return
