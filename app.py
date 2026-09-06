"""内部 OCR 服务面板。

同事在这里领密钥、看当前排队情况和最近的处理记录。
不需要注册账号，按访问设备 IP 登记，同一个 IP 永远拿到同一把密钥。
"""

import os
import socket
from datetime import datetime

import httpx
import pandas as pd
import streamlit as st

import db

PROXY_PORT = os.environ.get("OCR_HUB_PORT", "8000")
# 面板去哪里找网关。同机部署时就是本地回环；容器化后网关在另一个容器里，
# 用 OCR_HUB_PROXY_URL 指过去（compose 里设成 http://proxy:8000）。
PROXY_URL = os.environ.get("OCR_HUB_PROXY_URL", f"http://127.0.0.1:{PROXY_PORT}")
METRICS_URL = os.environ.get("OCR_HUB_METRICS_URL", "http://127.0.0.1:9105")
ADMIN_PASSWORD = os.environ.get("OCR_HUB_ADMIN_PASSWORD", "")
DEFAULT_MODEL = os.environ.get("OCR_HUB_DEFAULT_MODEL", "deepseek-ocr")
TRUST_PROXY_HEADERS = os.environ.get(
    "OCR_HUB_TRUST_PROXY_HEADERS", "0"
).lower() in ("1", "true", "yes")


@st.cache_data(ttl=300)
def lan_host() -> str:
    """探测本机在局域网里的地址，用来拼给同事看的调用示例。"""
    override = os.environ.get("OCR_HUB_HOST")
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def browser_ip():
    """读取当前浏览器连接的来源 IP；无效值交给发放逻辑拒绝。"""
    try:
        if TRUST_PROXY_HEADERS:
            forwarded = st.context.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
        return st.context.ip_address
    except Exception:
        return None


@st.cache_data(ttl=3)
def health():
    try:
        r = httpx.get(f"{PROXY_URL}/health", timeout=3.0)
        return r.json()
    except Exception:
        return None


@st.cache_data(ttl=2)
def host_metrics():
    try:
        r = httpx.get(f"{METRICS_URL}/metrics", timeout=2.5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


@st.cache_data(ttl=3)
def live_numbers():
    return db.stats_live(), db.stats_today()


@st.cache_data(ttl=3)
def recent_frame(limit: int, name: str):
    rows = db.recent(limit=limit, name=name or None)
    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        records.append(
            {
                "时间": datetime.fromtimestamp(r["enqueued_at"]).strftime("%H:%M:%S"),
                "密钥登记 IP": r["name"],
                "客户端 IP": r["client_ip"] or "未知",
                "模型": r["model"],
                "图片": r["image_count"],
                "排队": f"{r['queue_ms']/1000:.1f}s" if r["queue_ms"] is not None else "",
                "处理": f"{r['process_ms']/1000:.1f}s" if r["process_ms"] is not None else "",
                "状态": {
                    "queued": "排队中",
                    "running": "处理中",
                    "ok": "完成",
                    "error": "失败",
                    "aborted": "中断",
                }.get(r["status"], r["status"]),
                "说明": r["error"] or "",
            }
        )
    return pd.DataFrame(records)


@st.cache_data(ttl=3)
def client_stats_frame():
    rows = db.stats_by_client()
    records = []
    for r in rows:
        total = r["requests"] or 0
        records.append(
            {
                "客户端 IP": r["client_ip"],
                "请求": total,
                "图片": r["images"],
                "成功率": f"{r['ok'] / total * 100:.0f}%" if total else "—",
                "平均耗时": (
                    f"{r['avg_ms'] / 1000:.1f}s" if r["avg_ms"] is not None else "—"
                ),
                "最近访问": datetime.fromtimestamp(r["last_seen"]).strftime("%H:%M:%S"),
            }
        )
    return pd.DataFrame(records)


def gib(value) -> str:
    return f"{(value or 0) / (1024 ** 3):.1f} GB"


st.set_page_config(page_title="OCR 服务", layout="wide")
db.init_db()

host = lan_host()
base_url = f"http://{host}:{PROXY_PORT}/v1"
registered_ip = browser_ip()


# --- 侧栏：领密钥 -------------------------------------------------------

with st.sidebar:
    st.subheader("我的密钥")
    st.caption("自动读取当前访问设备的 IP。同一个 IP 重复登记，会返回同一把密钥。")

    if registered_ip:
        st.caption("当前设备 IP")
        st.code(registered_ip, language=None)
    else:
        st.warning("暂时无法读取当前设备 IP，请刷新页面后重试。")

    if st.session_state.get("my_key_ip") not in (None, registered_ip):
        st.session_state.pop("my_key", None)
        st.session_state.pop("key_is_new", None)
        st.session_state.pop("my_key_ip", None)

    if st.button(
        "登记本机 IP 并获取密钥",
        type="primary",
        width="stretch",
        disabled=not registered_ip,
    ):
        try:
            key, is_new = db.issue_key_for_ip(registered_ip)
            st.session_state["my_key"] = key
            st.session_state["key_is_new"] = is_new
            st.session_state["my_key_ip"] = registered_ip
        except ValueError as e:
            st.warning(str(e))

    if "my_key" in st.session_state:
        my_key = st.session_state["my_key"]
        st.caption("新建的密钥" if st.session_state.get("key_is_new") else "该 IP 已登记的密钥")
        st.code(my_key, language=None)

        st.divider()
        st.caption("Python 调用示例")
        st.code(
            f'''from openai import OpenAI
import base64

client = OpenAI(base_url="{base_url}", api_key="{my_key}")

with open("scan.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="{DEFAULT_MODEL}",
    messages=[{{"role": "user", "content": [
        {{"type": "text", "text": "<|grounding|>Convert the document to markdown."}},
        {{"type": "image_url",
         "image_url": {{"url": f"data:image/png;base64,{{b64}}"}}}},
    ]}}],
)
print(resp.choices[0].message.content)''',
            language="python",
        )

        st.caption("命令行验证")
        st.code(
            f'curl -H "Authorization: Bearer {my_key}" \\\n     {base_url}/models',
            language="bash",
        )


# --- 顶部：实时状态 -----------------------------------------------------

title_col, repo_col = st.columns(2, vertical_alignment="center")
title_col.title("OCR 服务")
repo_col.markdown(
    "[github.com/Reece-Lu/ollama-ocr-hub]"
    "(https://github.com/Reece-Lu/ollama-ocr-hub)"
)


@st.fragment(run_every="3s")
def live_status():
    live, today = live_numbers()
    hp = health()
    metrics = host_metrics()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("排队中", live["queued"])
    c2.metric("处理中", live["running"])
    c3.metric("今日已处理", today["images"] or today["total"])
    avg = today["avg_ms"]
    c4.metric("今日平均耗时", f"{avg/1000:.1f}s" if avg else "—")

    if hp is None:
        st.caption("网关未响应，服务可能没在运行")
    elif not hp.get("ollama"):
        st.caption("网关正常，但 Ollama 没连上")
    else:
        loaded = "、".join(hp.get("loaded", [])) if hp.get("loaded") else "无模型常驻"
        model_bytes = sum(m.get("size", 0) for m in hp.get("models", []))
        model_note = f"（{gib(model_bytes)}）" if model_bytes else ""
        st.caption(
            f"服务正常 · 已加载 {loaded}{model_note} · "
            f"空闲槽位 {hp['free_slots']}/{hp['max_parallel']}"
        )

    st.markdown("##### Mac 宿主机")
    h1, h2, h3, h4 = st.columns(4)
    if metrics:
        cpu = metrics.get("cpu", {})
        gpu = metrics.get("gpu", {})
        memory = metrics.get("memory", {})
        swap = metrics.get("swap", {})
        h1.metric(
            "CPU 使用率",
            f"{cpu.get('percent', 0):.0f}%",
            help=f"1 分钟负载：{cpu.get('load_1', 0):.2f}",
        )
        h2.metric(
            "GPU 活跃度",
            f"{gpu.get('device', 0):.0f}%" if gpu.get("available") else "—",
            help="Apple GPU 的 Device Utilization，由 macOS ioreg 读取",
        )
        h3.metric(
            "统一内存",
            gib(memory.get("used")),
            delta=(
                f"{memory.get('percent', 0):.0f}% 已用 / 共 {gib(memory.get('total'))}"
            ),
            delta_color="off",
        )
        h4.metric(
            "Swap",
            gib(swap.get("used")) if swap.get("available") else "—",
            delta=(
                f"{swap.get('percent', 0):.0f}% 已用"
                if swap.get("available")
                else "系统未返回"
            ),
            delta_color="off",
        )
    else:
        h1.metric("CPU 使用率", "—")
        h2.metric("GPU 活跃度", "—")
        h3.metric("统一内存", "—")
        h4.metric("Swap", "—")
        st.caption("宿主机指标服务未响应，请在 Mac 上运行 scripts/start-metrics.sh")


live_status()
st.divider()


# --- 下部：客户端与最近请求 ---------------------------------------------

st.subheader("今日客户端")
client_frame = client_stats_frame()
if client_frame.empty:
    st.caption("今天还没有客户端请求。")
else:
    st.dataframe(
        client_frame,
        width="stretch",
        hide_index=True,
        height=min(250, 38 * (len(client_frame) + 1)),
    )

st.divider()

head, tog = st.columns([4, 1])
head.subheader("最近请求")
only_mine = tog.checkbox("只看我的", value=False)

frame = recent_frame(100, registered_ip if only_mine else None)

if frame.empty:
    st.caption("还没有请求记录。领一把密钥，发第一张图试试。")
else:
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        height=420,
        column_config={
            "时间": st.column_config.TextColumn(width="small"),
            "密钥登记 IP": st.column_config.TextColumn(width="small"),
            "客户端 IP": st.column_config.TextColumn(width="small"),
            "图片": st.column_config.NumberColumn(width="small"),
            "排队": st.column_config.TextColumn(width="small"),
            "处理": st.column_config.TextColumn(width="small"),
            "状态": st.column_config.TextColumn(width="small"),
            "说明": st.column_config.TextColumn(width="medium"),
        },
    )


# --- 管理 ---------------------------------------------------------------

with st.expander("管理"):
    if not ADMIN_PASSWORD:
        st.caption("未设置 OCR_HUB_ADMIN_PASSWORD，管理功能关闭。")
    else:
        pw = st.text_input("管理口令", type="password", key="adminpw")
        if pw and pw == ADMIN_PASSWORD:
            keys = db.list_keys()
            if keys:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "登记 IP / 旧标识": k["name"],
                                "密钥": k["api_key"],
                                "领取时间": datetime.fromtimestamp(
                                    k["created_at"]
                                ).strftime("%m-%d %H:%M"),
                                "状态": "已停用" if k["revoked"] else "可用",
                            }
                            for k in keys
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )

            a, b = st.columns(2)
            with a:
                target = st.text_input("停用密钥（填登记 IP 或旧姓名）")
                if st.button("停用") and target.strip():
                    db.revoke_key(target)
                    st.cache_data.clear()
                    st.success(f"已停用 {target} 的密钥")
            with b:
                days = st.number_input("清理多少天前的记录", 1, 365, 30)
                if st.button("清理"):
                    n = db.purge_before(int(days))
                    st.cache_data.clear()
                    st.success(f"清理了 {n} 条记录")
        elif pw:
            st.warning("口令不对")
