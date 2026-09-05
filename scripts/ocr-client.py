#!/usr/bin/env python3
"""局域网 OCR 客户端 —— 拷到自己电脑上就能用，不用装任何依赖。

只用 Python 标准库，有 Python 3.8+ 就行。

    # 先设密钥（去面板 http://192.168.3.20:8501 填名字领）
    export OCR_HUB_KEY=sk-lan-xxxxxxxx

    python3 ocr-client.py 发票.png                  # 单张，结果打屏幕
    python3 ocr-client.py 扫描件/ --out 结果/         # 整个目录，每张存一个 .md
    python3 ocr-client.py *.png -c 2                # 并发 2 张
    python3 ocr-client.py --check                   # 测通不通

Windows 用 set 代替 export，或者直接 --key sk-lan-xxxx。
"""

import argparse
import base64
import concurrent.futures as cf
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 服务在哪台机器上。IP 变了就改这里，或者用 --url / OCR_HUB_URL 覆盖。
DEFAULT_URL = "http://192.168.3.20:8010"
DEFAULT_MODEL = "deepseek-ocr"
# <|grounding|> 是 DeepSeek-OCR 的专用语法，换别的模型要去掉
DEFAULT_PROMPT = "<|grounding|>Convert the document to markdown."

SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# 默认绕开系统代理。局域网服务走代理只会失败 —— 装了 Surge / Clash 之类的机器上
# HTTP_PROXY 会把发往 192.168.x.x 的请求也吞掉，症状是莫名其妙的 502/503。
# 真要走代理（比如经跳板机）加 --use-proxy。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def use_system_proxy() -> None:
    global _opener
    _opener = urllib.request.build_opener()


def post_json(url: str, payload: dict, key: str, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, key: str, timeout: float) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = urllib.request.Request(url, headers=headers)
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def explain(exc: Exception, url: str) -> str:
    """把底层异常翻译成能照着做的话。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("detail") or detail.get("error") or detail
            if isinstance(msg, dict):
                msg = msg.get("message", msg)
        except Exception:
            msg = exc.reason
        if exc.code == 401:
            return f"密钥无效（{msg}）。去面板重新领一把，或检查 OCR_HUB_KEY 有没有设对。"
        if exc.code == 403:
            return f"这个接口被网关封了：{msg}"
        if exc.code == 404:
            return f"模型不存在：{msg}。让管理员确认服务器上拉了这个模型。"
        return f"HTTP {exc.code}: {msg}"
    if isinstance(exc, urllib.error.URLError):
        return (f"连不上 {url}（{exc.reason}）。检查：\n"
                f"      1. 服务器那台机器开着、没休眠\n"
                f"      2. 自己和服务器在同一个局域网\n"
                f"      3. 地址对不对，用 --url 改")
    return f"{type(exc).__name__}: {exc}"


def collect_images(paths) -> list:
    out = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out += sorted(f for f in path.rglob("*") if f.suffix.lower() in SUFFIXES)
        elif path.is_file():
            out.append(path)
        else:
            print(f"[跳过] 找不到: {p}", file=sys.stderr)
    return out


def ocr_one(path: Path, args, key: str) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode()
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": args.prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
    }
    t0 = time.time()
    try:
        data = post_json(f"{args.url}/v1/chat/completions", payload, key, args.timeout)
        text = data["choices"][0]["message"]["content"]
        return {"path": path, "ok": True, "text": text, "dt": time.time() - t0}
    except Exception as exc:
        return {"path": path, "ok": False, "dt": time.time() - t0,
                "err": explain(exc, args.url)}


def check(args, key: str) -> int:
    print(f"测试 {args.url}\n")
    try:
        h = get_json(f"{args.url}/health", "", 8)
        print(f"  ✓ 连得上服务器")
        print(f"  {'✓' if h.get('ollama') else '✗'} 后端正常 · 空闲槽位 "
              f"{h.get('free_slots')}/{h.get('max_parallel')}")
    except Exception as exc:
        print(f"  ✗ {explain(exc, args.url)}")
        return 1
    if not key:
        print("\n  ! 没设密钥，跳过密钥检查。去面板领一把后：export OCR_HUB_KEY=sk-lan-xxxx")
        return 1
    try:
        d = get_json(f"{args.url}/v1/models", key, 15)
        names = [m["id"] for m in d.get("data", [])]
        print(f"  ✓ 密钥有效 · 可用模型: {', '.join(names) or '无'}")
        ok = any(n.split(":")[0] == args.model.split(":")[0] for n in names)
        print(f"  {'✓' if ok else '✗'} 模型 {args.model} {'可用' if ok else '不在列表里'}")
        return 0 if ok else 1
    except Exception as exc:
        print(f"  ✗ {explain(exc, args.url)}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="局域网 OCR 客户端（零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("images", nargs="*", help="图片文件或目录")
    ap.add_argument("--key", default=os.environ.get("OCR_HUB_KEY", ""),
                    help="API 密钥，也可用 OCR_HUB_KEY 环境变量")
    ap.add_argument("--url", default=os.environ.get("OCR_HUB_URL", DEFAULT_URL),
                    help=f"服务地址，默认 {DEFAULT_URL}")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("-c", "--concurrency", type=int, default=1, help="并发张数")
    ap.add_argument("--timeout", type=float, default=600.0, help="单张超时（秒）")
    ap.add_argument("--out", help="结果写到这个目录，每张图一个 .md")
    ap.add_argument("--check", action="store_true", help="只测连通性")
    ap.add_argument("--use-proxy", action="store_true",
                    help="走系统代理（默认绕开，局域网不该走代理）")
    args = ap.parse_args()
    args.url = args.url.rstrip("/")
    if args.use_proxy:
        use_system_proxy()

    if args.check:
        return check(args, args.key)
    if not args.key:
        return print("没有密钥。去面板领一把，然后：\n"
                     "  export OCR_HUB_KEY=sk-lan-xxxx      (Windows: set OCR_HUB_KEY=...)\n"
                     "  或者加参数 --key sk-lan-xxxx") or 2
    if not args.images:
        ap.error("要给至少一张图片，或用 --check 测连通性")

    images = collect_images(args.images)
    if not images:
        return print("没找到任何图片") or 1

    # 先快速探活。IP 打错的话 TCP 要等 70 多秒才超时，而且图片都白传了
    try:
        get_json(f"{args.url}/health", "", 6)
    except Exception as exc:
        print(f"连不上服务，没有发送任何图片。\n   {explain(exc, args.url)}")
        return 1

    print(f"{args.url} · {len(images)} 张图 · 并发 {args.concurrency}\n")
    t0 = time.time()
    if args.concurrency > 1:
        with cf.ThreadPoolExecutor(args.concurrency) as ex:
            results = list(ex.map(lambda p: ocr_one(p, args, args.key), images))
    else:
        results = [ocr_one(p, args, args.key) for p in images]
    total = time.time() - t0

    outdir = Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for r in results:
        if not r["ok"]:
            print(f"── {r['path'].name}  失败 ({r['dt']:.1f}s)\n   {r['err']}\n")
            continue
        ok += 1
        print(f"── {r['path'].name}  {r['dt']:.1f}s · {len(r['text'])} 字")
        if outdir:
            f = outdir / (r["path"].stem + ".md")
            f.write_text(r["text"], encoding="utf-8")
            print(f"   → {f}")
        else:
            print("   " + r["text"].replace("\n", "\n   "))
        print()

    times = [r["dt"] for r in results if r["ok"]]
    line = f"成功 {ok}/{len(results)} · 总耗时 {total:.1f}s"
    if times:
        line += f" · 单张平均 {sum(times)/len(times):.1f}s"
    print(line)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
