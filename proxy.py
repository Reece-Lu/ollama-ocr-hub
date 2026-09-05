"""对局域网暴露的鉴权网关。

Ollama 本身不带认证，所以它只监听 127.0.0.1，由这个进程对外。
除了鉴权，它还负责：限制并发、记录排队和处理耗时、封掉危险接口。
"""

import asyncio
import ipaddress
import json
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import db

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
STRIP_TOKENS = os.environ.get("OCR_HUB_STRIP_TOKENS", "1") not in ("0", "false", "no")
MAX_PARALLEL = int(os.environ.get("OCR_HUB_PARALLEL", "2"))
TIMEOUT = float(os.environ.get("OCR_HUB_TIMEOUT", "600"))
TRUST_PROXY_HEADERS = os.environ.get(
    "OCR_HUB_TRUST_PROXY_HEADERS", "0"
).lower() in ("1", "true", "yes")

# Ollama 暴露了拉取/推送/删除模型的接口，一律拦下
BLOCKED_PREFIXES = (
    "/api/pull",
    "/api/push",
    "/api/delete",
    "/api/create",
    "/api/copy",
    "/api/blobs",
)

gate = asyncio.Semaphore(MAX_PARALLEL)
client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    db.init_db()
    n = db.reset_stale()
    if n:
        print(f"清理了 {n} 条上次残留的未完成记录")
    client = httpx.AsyncClient(timeout=TIMEOUT)
    print(f"网关已启动 -> {OLLAMA_BASE}，并发上限 {MAX_PARALLEL}")
    yield
    await client.aclose()


app = FastAPI(title="OCR 服务网关", lifespan=lifespan)


def _normalize_ip(value: str) -> str:
    """规范化 IP；解析不了时保留原值，便于排查代理配置。"""
    value = (value or "").strip()
    if not value:
        return "未知"
    try:
        parsed = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        return str(parsed)
    except ValueError:
        return value[:64]


def get_client_ip(request: Request) -> str:
    """取真实来源地址。

    默认只信任 TCP 对端，避免客户端伪造 X-Forwarded-For。只有在 macOS
    宿主机或其他可信反向代理主动补这个头时，才应开启代理头支持。
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return _normalize_ip(forwarded.split(",", 1)[0])
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return _normalize_ip(real_ip)
    return _normalize_ip(request.client.host if request.client else "")


# --- 鉴权 ---------------------------------------------------------------


async def require_key(
    authorization: str = Header(None),
    x_api_key: str = Header(None),
):
    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()

    row = await asyncio.to_thread(db.lookup_key, key)
    if row is None:
        raise HTTPException(status_code=401, detail="密钥无效或已停用")
    return {"name": row["name"], "api_key": row["api_key"]}


@app.middleware("http")
async def block_dangerous(request: Request, call_next):
    if request.url.path.startswith(BLOCKED_PREFIXES):
        return JSONResponse(
            {"error": "该接口已在网关关闭"}, status_code=403
        )
    return await call_next(request)


# --- 请求体分析 ---------------------------------------------------------


def count_images(payload: dict):
    """数出请求里带了几张图、原始字节数大概多少。只统计大小，不留存图片内容。"""
    n, size = 0, 0

    # Ollama 原生格式: messages[].images = [base64, ...]
    for msg in payload.get("messages") or []:
        for b64 in msg.get("images") or []:
            n += 1
            size += len(b64) * 3 // 4
        content = msg.get("content")
        # OpenAI 格式: content 是 [{type: image_url, image_url: {url: "data:..."}}]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    n += 1
                    url = (part.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        size += len(url.split(",", 1)[-1]) * 3 // 4

    # /api/generate 格式: 顶层 images
    for b64 in payload.get("images") or []:
        n += 1
        size += len(b64) * 3 // 4

    return n, size


# --- 特殊 token 清洗 ---------------------------------------------------
#
# DeepSeek-OCR 会把模板 token 漏进输出，实测见过 <|im_end|>、<|md_start|>，
# 以及没有闭合的残缺形式 <|im_begin| 和 <|im_editpolicy="200"。
# grounding 模式还会吐 <|ref|>文字<|/ref|><|det|>[[坐标]]<|/det|>。
#
# 注意别误伤 <table><td colspan="1"> 这类 HTML —— 那是模型的表格结构化
# 输出，是有效内容，只认 <|...|> 这种形式。

# 定位框整块丢掉（里面是坐标，不是文字）
_DET_RE = re.compile(r"<\|det\|>.*?<\|/det\|>", re.S)
# 其余 token 只去标记、留中间的文字（<|ref|>正文<|/ref|> 要保住正文）。
# 尾部的 (?:\|>|\|)? 是为了兼容残缺形式。
_TOK_RE = re.compile(
    r"<\|/?[A-Za-z0-9_]+(?:=\"[^\"\n]*\"|=[^\s|>\n]*)?(?:\|>|\|)?"
)
_BLANKS_RE = re.compile(r"\n{3,}")

# 残缺 token 缓冲的上限：超过就不再等闭合，直接放行，避免把整个响应吞掉
_HOLD_LIMIT = 4096


def strip_tokens(text: str, normalize: bool = True) -> str:
    if not text:
        return text
    text = _DET_RE.sub("", text)
    text = _TOK_RE.sub("", text)
    if normalize:
        text = _BLANKS_RE.sub("\n\n", text).strip()
    return text


class TokenStripper:
    """流式清洗。token 会跨 chunk 断开，所以尾部可能不完整的部分先扣住不发。"""

    def __init__(self):
        self.buf = ""

    def _hold_index(self):
        """返回该从哪个位置起扣住不发；None 表示整个缓冲都可以放行。"""
        b = self.buf
        # 未闭合的 <|det|> 块，整块扣住（里面的坐标要连着标记一起丢）
        d = b.rfind("<|det|>")
        if d != -1 and "<|/det|>" not in b[d:]:
            return d
        # 尾部可能是半截 token。从最后一个 "<" 起算 —— 单独一个 "<" 也得扣，
        # 它可能是下一个 chunk 里 "<|" 的前半截。
        i = b.rfind("<")
        if i == -1:
            return None
        tail = b[i:]
        if tail == "<":
            return i
        if tail.startswith("<|") and "|>" not in tail:
            return i
        return None

    def feed(self, text: str) -> str:
        self.buf += text
        hold = self._hold_index()
        if hold is not None and len(self.buf) - hold > _HOLD_LIMIT:
            hold = None       # 扣太久了，八成是残缺 token，放行
        if hold is None:
            head, self.buf = self.buf, ""
        else:
            head, self.buf = self.buf[:hold], self.buf[hold:]
        return strip_tokens(head, normalize=False)

    def flush(self) -> str:
        out = strip_tokens(self.buf, normalize=False)
        self.buf = ""
        return out


def clean_response(data: dict) -> int:
    """就地清洗非流式响应，返回清洗后的字符数。"""
    total = 0
    for ch in data.get("choices") or []:                 # OpenAI 格式
        msg = ch.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"] = strip_tokens(msg["content"])
            total += len(msg["content"])
    msg = data.get("message")                             # Ollama /api/chat
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        msg["content"] = strip_tokens(msg["content"])
        total += len(msg["content"])
    if isinstance(data.get("response"), str):             # Ollama /api/generate
        data["response"] = strip_tokens(data["response"])
        total += len(data["response"])
    return total


def _is_final(obj: dict) -> bool:
    if obj.get("done") is True:                           # Ollama 原生
        return True
    for ch in obj.get("choices") or []:                   # OpenAI SSE
        if ch.get("finish_reason"):
            return True
    return False


def _clean_chunk_obj(obj: dict, st: "TokenStripper") -> int:
    """就地清洗流式响应里的一个 JSON 块，返回本块产出的字符数。

    结束块要把缓冲里扣着的尾巴一起吐出来，否则那部分内容就丢了。
    """
    final = _is_final(obj)
    tail = st.flush() if final else ""
    n = 0

    for ch in obj.get("choices") or []:                   # OpenAI SSE
        delta = ch.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("content"), str):
                delta["content"] = st.feed(delta["content"]) + tail
                tail = ""
                n += len(delta["content"])
            elif tail:
                delta["content"] = tail
                tail = ""
                n += len(delta["content"])

    msg = obj.get("message")                              # Ollama /api/chat
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        msg["content"] = st.feed(msg["content"]) + tail
        tail = ""
        n += len(msg["content"])

    if isinstance(obj.get("response"), str):              # Ollama /api/generate
        obj["response"] = st.feed(obj["response"]) + tail
        tail = ""
        n += len(obj["response"])

    return n


def _clean_line(line: bytes, st: "TokenStripper"):
    """清洗流里的一行（OpenAI 是 SSE 的 data: 行，Ollama 是 NDJSON）。"""
    body = line.strip()
    if not body:
        return line, 0
    prefix = b""
    if body.startswith(b"data:"):
        prefix, body = b"data: ", body[5:].strip()
    if not body.startswith(b"{"):                         # [DONE] 之类原样放行
        return line, 0
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return line, 0
    n = _clean_chunk_obj(obj, st)
    eol = line[len(line.rstrip(b"\r\n")):]
    return prefix + json.dumps(obj, ensure_ascii=False).encode() + eol, n


def extract_output(data: dict) -> str:
    """从上游响应里取出文本，用来记录输出长度。"""
    try:
        if "choices" in data:
            return data["choices"][0]["message"]["content"] or ""
        if "message" in data:
            return data["message"].get("content") or ""
        return data.get("response") or ""
    except (KeyError, IndexError, TypeError):
        return ""


# --- 核心转发 -----------------------------------------------------------


async def forward(request: Request, key: dict, upstream_path: str):
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    n_img, img_bytes = count_images(payload)
    model = payload.get("model", "")
    streaming = bool(payload.get("stream"))

    rid = await asyncio.to_thread(
        db.log_enqueue,
        key["name"],
        model,
        n_img,
        img_bytes,
        get_client_ip(request),
    )

    await gate.acquire()
    holding = True
    try:
        await asyncio.to_thread(db.log_start, rid)
        url = f"{OLLAMA_BASE}{upstream_path}"
        headers = {"Content-Type": "application/json"}

        if streaming:
            req = client.build_request("POST", url, content=raw, headers=headers)
            resp = await client.send(req, stream=True)
            holding = False  # 信号量交给生成器负责释放
            return StreamingResponse(
                stream_and_log(resp, rid, STRIP_TOKENS),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        resp = await client.post(url, content=raw, headers=headers)
        if resp.status_code >= 400:
            await asyncio.to_thread(
                db.log_finish, rid, "error", 0, f"上游 {resp.status_code}"
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        data = resp.json()
        if STRIP_TOKENS:
            out_chars = clean_response(data)
        else:
            out_chars = len(extract_output(data))
        await asyncio.to_thread(db.log_finish, rid, "ok", out_chars, None)
        return JSONResponse(data)

    except Exception as exc:
        await asyncio.to_thread(db.log_finish, rid, "error", 0, str(exc)[:500])
        raise HTTPException(status_code=502, detail=f"转发失败: {exc}")
    finally:
        if holding:
            gate.release()


async def stream_and_log(resp: httpx.Response, rid: int, strip: bool = False):
    chars = 0
    st = TokenStripper() if strip else None
    pending = b""
    try:
        async for chunk in resp.aiter_bytes():
            if st is None:
                chars += len(chunk)
                yield chunk
                continue
            # 按行切：token 可能跨 chunk 断开，半行不能解析
            pending += chunk
            out = bytearray()
            while True:
                nl = pending.find(b"\n")
                if nl == -1:
                    break
                line, pending = pending[: nl + 1], pending[nl + 1 :]
                cleaned, n = _clean_line(line, st)
                chars += n
                out += cleaned
            if out:
                yield bytes(out)
        if st is not None and pending:
            cleaned, n = _clean_line(pending, st)
            chars += n
            if cleaned:
                yield cleaned
        await asyncio.to_thread(db.log_finish, rid, "ok", chars, None)
    except Exception as exc:
        await asyncio.to_thread(db.log_finish, rid, "error", chars, str(exc)[:500])
        raise
    finally:
        await resp.aclose()
        gate.release()


# --- 路由 ---------------------------------------------------------------


@app.post("/v1/chat/completions")
async def openai_chat(request: Request, key=Depends(require_key)):
    return await forward(request, key, "/v1/chat/completions")


@app.post("/api/chat")
async def native_chat(request: Request, key=Depends(require_key)):
    return await forward(request, key, "/api/chat")


@app.post("/api/generate")
async def native_generate(request: Request, key=Depends(require_key)):
    return await forward(request, key, "/api/generate")


@app.get("/v1/models")
async def list_models(key=Depends(require_key)):
    resp = await client.get(f"{OLLAMA_BASE}/v1/models")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.get("/api/tags")
async def list_tags(key=Depends(require_key)):
    resp = await client.get(f"{OLLAMA_BASE}/api/tags")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.get("/api/ps")
async def loaded_models(key=Depends(require_key)):
    resp = await client.get(f"{OLLAMA_BASE}/api/ps")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.get("/health")
async def health():
    """面板用它判断 Ollama 是否活着，不需要密钥。"""
    ollama_ok = False
    loaded = []
    models = []
    try:
        resp = await client.get(f"{OLLAMA_BASE}/api/ps", timeout=3.0)
        ollama_ok = resp.status_code == 200
        if ollama_ok:
            for model in resp.json().get("models", []):
                loaded.append(model["name"])
                models.append(
                    {
                        "name": model["name"],
                        "size": model.get("size", 0),
                        "size_vram": model.get("size_vram", 0),
                        "context_length": (
                            model.get("details") or {}
                        ).get("context_length"),
                    }
                )
    except Exception:
        pass
    return {
        "ollama": ollama_ok,
        "loaded": loaded,
        "models": models,
        "max_parallel": MAX_PARALLEL,
        "free_slots": gate._value,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("OCR_HUB_BIND", "0.0.0.0"),
        port=int(os.environ.get("OCR_HUB_PORT", "8000")),
    )
