# ollama-ocr-hub

局域网内部的 OCR 服务：Mac 上跑 Ollama，同事凭密钥调用，附一个看排队和用量的面板。

## 架构

```
同事的程序  ──HTTP + 密钥──>  网关 :8000  ──>  Ollama :11434（仅本地）
                                 │
                                 ↓ 写
                            data/hub.db
                                 ↑ 读
同事的浏览器 ──> Mac 面板入口 :8501 ──> Streamlit 容器 :8502
                                 │
                                 └─> Mac 指标服务 :9105（仅本机）
```

Ollama 自己不带认证，所以它只监听 `127.0.0.1`，对外一律走网关。网关顺便做三件事：
限制并发、记录每次请求的排队和处理耗时、封掉拉取和删除模型的接口。

面板和网关是两个独立进程，通过同一个 SQLite 文件（WAL 模式）共享状态，
不需要额外的进程间通信。

| 端口 | 服务 | 谁访问 | 监听地址 |
|---|---|---|---|
| 11434 | Ollama | 只有网关 | 127.0.0.1 |
| 8000 | 网关 | 同事的程序 | 0.0.0.0 |
| 8501 | Mac 原生面板入口 | 同事的浏览器 | 0.0.0.0 |
| 8502 | Streamlit 面板容器 | 只有 Mac 原生入口 | 127.0.0.1 |
| 9105 | Mac 指标服务 | 只有本机和 Docker Desktop | 127.0.0.1 |

## 安装

需要 Python 3.10+ 和已装好的 Ollama。

```bash
git clone <你的仓库地址>
cd ollama-ocr-hub
./scripts/setup.sh
```

编辑 `.env`，至少把 `OCR_HUB_HOST` 改成这台机器的局域网 IP：

```
OCR_HUB_HOST=192.168.55.10
OCR_HUB_ADMIN_PASSWORD=随便设一个
```

配置 Ollama 并拉模型：

```bash
./scripts/ollama-env.sh     # 写 launchctl 变量
# 退出 Ollama 应用再重新打开，变量才生效
ollama pull deepseek-ocr
```

## 运行

两个终端：

```bash
./scripts/start-proxy.sh    # 网关
./scripts/start-panel.sh    # 面板
```

然后浏览器打开 `http://192.168.55.10:8501`。

## 用 Docker 跑（可选）

网关和面板可以放进容器，**Ollama 必须留在宿主机原生运行** ——
macOS 上 Docker 跑在 Linux 虚拟机里，拿不到 Metal，Ollama 进容器会退化成纯 CPU 推理。

```bash
docker compose up -d --build
./scripts/start-metrics.sh  # 同时启动 Mac 指标服务和保留真实 IP 的面板入口
```

容器通过 `host.docker.internal:11434` 回连宿主机的 Ollama。Docker Desktop
会从宿主机侧代理这个连接，所以 Ollama 可以继续只监听 `127.0.0.1`，
不用为了容器而对局域网敞开，鉴权模型不受影响。

几个要注意的：

- **`OCR_HUB_HOST` 必须显式设置。** 容器里自动探测 IP 只会得到容器自己的地址。
- 数据库放在命名卷 `hubdata` 里，不用宿主机 bind mount ——
  macOS 的 VirtioFS 对 SQLite 文件锁支持不可靠，而 WAL 模式重度依赖它。
  备份用 `docker compose exec proxy sqlite3 /data/hub.db .dump > backup.sql`。
- 网关对外发布在 **8010**（容器内仍是 8000），因为宿主机 8000 被别的项目占了。
  要改回 8000，把 `docker-compose.yml` 里 proxy 的端口映射和 panel 的
  `OCR_HUB_PORT` 一起改掉。
- 这套 `host.docker.internal` 的行为是 Docker Desktop for Mac/Windows 独有的，
  **Linux 上不成立**。真要挪到 Linux + NVIDIA 的机器，那时 Ollama 也一并进容器
  （用 `nvidia-container-toolkit` 透传 GPU）反而更合适。
- CPU、GPU、统一内存和 Swap 必须由 `scripts/start-metrics.sh` 在 Mac 原生采集。
  指标服务只监听 `127.0.0.1:9105`，不会直接暴露给同事。GPU 活跃度来自
  Apple GPU 驱动的 `Device Utilization %`，不需要 `sudo`。
- Streamlit 容器只发布到宿主机回环地址 `127.0.0.1:8502`。同事访问的 8501
  由同一个 Mac 原生进程转发并写入可信的 `X-Forwarded-For`，这样登记页面才能
  取得真实局域网客户端 IP，而不是 Docker Desktop 的 `192.168.65.1`。

常用命令：

```bash
docker compose logs -f proxy    # 看网关日志
docker compose restart panel    # 改完 .env 后重启
docker compose down             # 停掉（数据卷保留）
```

## 客户端 IP 统计

网关会给每条新请求记录 TCP 来源 IP，面板的「今日客户端」会按 IP 显示请求数、
图片数、成功率、平均耗时和最后访问时间。升级前的历史记录没有来源地址，会显示为
「未知」。

Docker Desktop 的端口转发会经过宿主机后端进程，容器通常只能看到 Docker 网关
IP。当前 Docker 配置已经让 `scripts/start-metrics.sh` 同时启动一个 Mac 原生面板
入口，由它写入可信的 `X-Forwarded-For`。若以后换成自己的 Caddy/Nginx，也需要设置：

```text
OCR_HUB_TRUST_PROXY_HEADERS=1
```

没有可信反向代理时不要开启这个选项，否则客户端可以伪造统计里的 IP。


## 同事怎么用

1. 打开面板，确认自动识别的本机 IP，点「登记本机 IP 并获取密钥」
2. 复制页面上给出的调用示例，改一下图片路径

同一个 IP 每次领到的是同一把密钥，重复点没有副作用。IP 来自浏览器与面板的
连接地址；如果前面增加了反向代理，需要让代理保留真实客户端 IP。

```python
from openai import OpenAI
import base64

client = OpenAI(base_url="http://192.168.55.10:8000/v1", api_key="sk-lan-xxxxx")

with open("scan.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="deepseek-ocr",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "<|grounding|>Convert the document to markdown."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]}],
)
print(resp.choices[0].message.content)
```

`<|grounding|>` 是 DeepSeek-OCR 的专用提示词语法，换别的模型要改掉。

## 自己测

`scripts/test-ocr.py` 拿自己的文档跑网关，看识别结果和耗时。
密钥和网关地址都会自动找（容器的库优先，端口从 `docker-compose.yml` 的映射推断）。

```bash
./.venv/bin/python scripts/test-ocr.py --check          # 先体检网关
./.venv/bin/python scripts/test-ocr.py 发票.png          # 单张，结果打屏幕
./.venv/bin/python scripts/test-ocr.py 扫描件/ --out 结果/  # 整个目录，每张存一个 .md
./.venv/bin/python scripts/test-ocr.py 扫描件/ -c 2       # 并发 2，看排队效果
./.venv/bin/python scripts/test-ocr.py a.png --stream    # 流式，顺带报首字延迟
```

`--check` 会验网关存活、Ollama 连通、无密钥被拒 401、`/api/pull` 被封 403、
目标模型是否已拉。退出码 0/1，可以直接塞进 CI。

密钥优先级：`--key` > `OCR_HUB_KEY` 环境变量 > 容器里的库 > 本机的库。
换模型或提示词用 `--model` / `--prompt`。

## 同事那台电脑怎么调

把 `scripts/ocr-client.py` 一个文件发给同事就行 —— **零依赖**，只用 Python 标准库，
有 Python 3.8+ 就能跑，不用装 httpx 也不用建虚拟环境。

```bash
export OCR_HUB_KEY=sk-lan-xxxxxxxx        # Windows: set OCR_HUB_KEY=sk-lan-xxxx

python3 ocr-client.py --check             # 先测通不通
python3 ocr-client.py 发票.png             # 单张，结果打屏幕
python3 ocr-client.py 扫描件/ --out 结果/    # 整个目录，每张存一个 .md
python3 ocr-client.py *.png -c 2          # 并发 2 张
```

密钥去面板 `http://192.168.3.20:8501`，登记页面自动识别的本机 IP 后领取。服务器地址已经写死在脚本开头的
`DEFAULT_URL` 里，机器 IP 变了就改那一行，或者用 `--url` / `OCR_HUB_URL` 覆盖。

**脚本默认绕开系统代理。** 装了 Surge / Clash 之类的机器上，`HTTP_PROXY` 会把发往
`192.168.x.x` 的请求也吞掉，症状是莫名其妙的 502/503 —— 这个坑很常见，所以默认就绕开了。
真要走代理（比如经跳板机）加 `--use-proxy`。

出错会直接说该怎么办，不是甩一个 traceback：

```
── 发票.png  失败 (0.0s)
   密钥无效（密钥无效或已停用）。去面板重新领一把，或检查 OCR_HUB_KEY 有没有设对。
```

不想用脚本、要嵌进自己程序的，用 OpenAI SDK 也行，见上面「同事怎么用」那节。

## 接口

需要密钥（`Authorization: Bearer <key>` 或 `X-API-Key: <key>`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 格式，推荐 |
| POST | `/api/chat` | Ollama 原生格式 |
| POST | `/api/generate` | Ollama 原生格式 |
| GET | `/v1/models` | 模型列表 |
| GET | `/api/tags` | 模型列表（原生格式）|
| GET | `/api/ps` | 当前加载的模型 |

不需要密钥：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 面板用来判断服务是否正常 |

一律返回 403：`/api/pull`、`/api/push`、`/api/delete`、`/api/create`、`/api/copy`、`/api/blobs`。
这些接口能下载和删除模型，不该对局域网开放。

## 输出清洗

DeepSeek-OCR 会把模板 token 漏进输出，实测见过 `<|im_end|>`、`<|md_start|>`，
以及没有闭合的残缺形式 `<|im_begin|` 和 `<|im_editpolicy="200"`。
grounding 模式还会吐 `<|ref|>文字<|/ref|><|det|>[[坐标]]<|/det|>`。

网关默认在转发前把这些剥掉，同事拿到的就是干净文本，不用各自写清洗代码。
`<|ref|>` 里的正文会保留，`<|det|>` 的坐标连标记一起丢弃。

**不碰 `<table><td colspan="1">` 这类 HTML** —— 那是模型的表格结构化输出，是有效内容。

流式同样会清洗。特殊 token 可能跨 chunk 断开（`<|im` 一块、`_end|>` 下一块），
所以尾部可能不完整的部分会先扣住，等确认完整再发；扣住的内容在结束块补发。

要原样透传就设 `OCR_HUB_STRIP_TOKENS=0`。

## 配置项

全部走环境变量，见 `.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama 地址 |
| `OCR_HUB_PORT` | `8000` | 网关端口 |
| `OCR_HUB_BIND` | `0.0.0.0` | 网关监听地址 |
| `OCR_HUB_PARALLEL` | `2` | 并发上限，要和 `OLLAMA_NUM_PARALLEL` 一致 |
| `OCR_HUB_TIMEOUT` | `600` | 单次请求超时（秒）|
| `OCR_HUB_HOST` | 自动探测 | 面板示例代码里显示的地址 |
| `OCR_HUB_DEFAULT_MODEL` | `deepseek-ocr` | 示例代码里的模型名 |
| `OCR_HUB_STRIP_TOKENS` | `1` | 剥掉模型漏出的特殊 token，设 `0` 关闭 |
| `OCR_HUB_TRUST_PROXY_HEADERS` | `0` | 是否信任反向代理写入的客户端 IP 请求头 |
| `OCR_HUB_METRICS_URL` | `http://127.0.0.1:9105` | 面板读取 Mac 指标的地址 |
| `OCR_HUB_METRICS_BIND` | `127.0.0.1` | Mac 指标服务监听地址 |
| `OCR_HUB_METRICS_PORT` | `9105` | Mac 指标服务端口 |
| `OCR_HUB_PANEL_BIND` | `0.0.0.0` | Mac 原生面板入口监听地址 |
| `OCR_HUB_PANEL_PORT` | `8501` | 同事浏览器访问的面板端口 |
| `OCR_HUB_PANEL_UPSTREAM` | `http://127.0.0.1:8502` | Streamlit 容器在宿主机上的回环地址 |
| `OCR_HUB_ADMIN_PASSWORD` | 空 | 不设则面板管理区块不可用 |
| `OCR_HUB_DB` | `data/hub.db` | 数据库路径 |

## 数据

只存两张表：`keys`（登记 IP/旧标识、密钥）和 `requests`（密钥登记标识、实际来源 IP、什么模型、几张图、耗时、状态）。

**图片内容不落盘，只记录字节数。**

## 已知限制

- 任何能访问面板的人都能按来源 IP 领密钥。IP 只能用于登记和区分局域网设备，
  不能当作可靠的身份认证；内部信任度不够的话，
  在 `app.py` 的发放逻辑前加一道共享口令即可。
- 网关是 HTTP，密钥在局域网里明文传输。要 HTTPS 的话前面再套一层 Caddy。
- 面板每 3 秒轮询一次数据库。人多了可以把 `st.cache_data` 的 `ttl` 调大。
- Mac 合盖休眠服务就断。系统设置里关掉自动睡眠，或用 `caffeinate -s` 挂着。
- 局域网 IP 要在路由器上做 DHCP 静态绑定，否则同事的配置过几天就失效。
