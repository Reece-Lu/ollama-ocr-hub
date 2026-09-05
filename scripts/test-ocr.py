#!/usr/bin/env python3
"""命令行 OCR 测试工具。拿自己的图片试网关，看识别结果和耗时。

    ./.venv/bin/python scripts/test-ocr.py 发票.png
    ./.venv/bin/python scripts/test-ocr.py 扫描件/ --out 结果/
    ./.venv/bin/python scripts/test-ocr.py a.png b.png c.png -c 3   # 测排队
    ./.venv/bin/python scripts/test-ocr.py --check                  # 只体检网关

密钥按这个顺序找：--key 参数 -> OCR_HUB_KEY 环境变量 -> 容器里的库 -> 本机的库。
"""

import argparse
import base64
import concurrent.futures as cf
import json
import mimetypes
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("缺 httpx。用项目的虚拟环境跑：./.venv/bin/python scripts/test-ocr.py ...")

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
DEFAULT_PROMPT = "<|grounding|>Convert the document to markdown."


# --- 找配置 -------------------------------------------------------------


def read_env_file() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def find_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key
    if os.environ.get("OCR_HUB_KEY"):
        return os.environ["OCR_HUB_KEY"]

    # 容器里的库（命名卷）和本机的库是两个不同的文件，容器优先
    snippet = "import db; ks=db.list_keys(); print(ks[0]['api_key'] if ks else '')"
    try:
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "panel", "python", "-c", snippet],
            cwd=ROOT, capture_output=True, text=True, timeout=25,
        )
        k = out.stdout.strip()
        if k.startswith("sk-lan-"):
            print(f"[密钥] 取自容器: {k[:14]}…")
            return k
    except Exception:
        pass

    try:
        sys.path.insert(0, str(ROOT))
        import db  # noqa: E402
        ks = db.list_keys()
        if ks:
            k = ks[0]["api_key"]
            print(f"[密钥] 取自本机库: {k[:14]}…")
            return k
    except Exception:
        pass

    sys.exit(
        "找不到密钥。先去面板领一把，然后：\n"
        "  --key sk-lan-xxxx   或   export OCR_HUB_KEY=sk-lan-xxxx"
    )


def default_base_url() -> str:
    env = read_env_file()
    # 容器把网关发布在宿主机的哪个端口，以 compose 里的映射为准
    port = env.get("OCR_HUB_PORT", "8000")
    compose = ROOT / "docker-compose.yml"
    if compose.exists():
        for line in compose.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- \"") and ":8000\"" in s:
                port = s.split("\"")[1].split(":")[0]
                break
    return f"http://127.0.0.1:{port}"


# --- 请求 ---------------------------------------------------------------


def collect_images(paths: list[str]) -> list[Path]:
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


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_payload(path: Path, args) -> dict:
    return {
        "model": args.model,
        "stream": args.stream,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": args.prompt},
            {"type": "image_url", "image_url": {"url": data_url(path)}},
        ]}],
    }


def ocr_one(path: Path, args, key: str) -> dict:
    url = f"{args.url}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    payload = build_payload(path, args)
    t0 = time.time()
    try:
        if args.stream:
            text, first = "", None
            with httpx.stream("POST", url, json=payload, headers=headers,
                              timeout=args.timeout, trust_env=False) as r:
                if r.status_code != 200:
                    r.read()
                    return {"path": path, "ok": False, "dt": time.time() - t0,
                            "err": f"HTTP {r.status_code}: {r.text[:200]}"}
                for line in r.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        continue
                    try:
                        obj = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    for ch in obj.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece and first is None:
                            first = time.time() - t0
                        text += piece
            return {"path": path, "ok": True, "text": text,
                    "dt": time.time() - t0, "ttfb": first}

        r = httpx.post(url, json=payload, headers=headers,
                       timeout=args.timeout, trust_env=False)
        dt = time.time() - t0
        if r.status_code != 200:
            return {"path": path, "ok": False, "dt": dt,
                    "err": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"path": path, "ok": True, "dt": dt,
                "text": r.json()["choices"][0]["message"]["content"], "ttfb": None}
    except Exception as exc:
        return {"path": path, "ok": False, "dt": time.time() - t0,
                "err": f"{type(exc).__name__}: {exc}"}


# --- 网关体检 -----------------------------------------------------------


def gateway_check(args, key: str) -> int:
    print(f"体检 {args.url}\n")
    bad = 0

    def line(name, ok, extra=""):
        nonlocal bad
        bad += not ok
        print(f"  {'✓' if ok else '✗'} {name}{'  ' + extra if extra else ''}")

    try:
        h = httpx.get(f"{args.url}/health", timeout=5, trust_env=False).json()
        line("网关存活", True)
        line("Ollama 连通", bool(h.get("ollama")),
             f"常驻模型: {h.get('loaded') or '无'} · 空闲槽位 "
             f"{h.get('free_slots')}/{h.get('max_parallel')}")
    except Exception as exc:
        line("网关存活", False, str(exc))
        print("\n网关没起来？ docker compose up -d")
        return 1

    try:
        r = httpx.get(f"{args.url}/v1/models", timeout=5, trust_env=False)
        line("无密钥被拒", r.status_code == 401, f"实得 {r.status_code}")
    except Exception as exc:
        line("无密钥被拒", False, str(exc))

    try:
        r = httpx.get(f"{args.url}/v1/models", timeout=10, trust_env=False,
                      headers={"Authorization": f"Bearer {key}"})
        names = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else []
        line("密钥可用", r.status_code == 200, f"可用模型: {', '.join(names) or '无'}")
        line(f"目标模型 {args.model} 已就绪",
             any(n.split(":")[0] == args.model.split(":")[0] for n in names),
             "" if names else "先 ollama pull")
    except Exception as exc:
        line("密钥可用", False, str(exc))

    for path in ("/api/pull", "/api/delete"):
        try:
            r = httpx.post(f"{args.url}{path}", timeout=5, trust_env=False)
            line(f"{path} 被封", r.status_code == 403, f"实得 {r.status_code}")
        except Exception as exc:
            line(f"{path} 被封", False, str(exc))

    print("\n" + ("全部通过" if not bad else f"{bad} 项异常"))
    return 1 if bad else 0


# --- 主流程 -------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="OCR 网关测试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("images", nargs="*", help="图片文件或目录")
    ap.add_argument("--key", help="API 密钥，不给就自动找")
    ap.add_argument("--url", default=None, help="网关地址，默认从 docker-compose.yml 推断")
    ap.add_argument("--model", default=None, help="模型名")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="提示词")
    ap.add_argument("--stream", action="store_true", help="流式，顺带测首字延迟")
    ap.add_argument("-c", "--concurrency", type=int, default=1, help="并发数，用来测排队")
    ap.add_argument("--timeout", type=float, default=600.0, help="单次超时（秒）")
    ap.add_argument("--out", help="把识别结果写到这个目录，每张图一个 .md")
    ap.add_argument("--check", action="store_true", help="只体检网关，不跑 OCR")
    args = ap.parse_args()

    env = read_env_file()
    args.url = (args.url or os.environ.get("OCR_HUB_URL") or default_base_url()).rstrip("/")
    args.model = args.model or env.get("OCR_HUB_DEFAULT_MODEL", "deepseek-ocr")
    key = find_key(args.key)

    if args.check:
        return gateway_check(args, key)

    if not args.images:
        ap.error("要给至少一张图片，或用 --check 只做体检")

    images = collect_images(args.images)
    if not images:
        return print("没有找到任何图片") or 1

    print(f"网关 {args.url} · 模型 {args.model} · {len(images)} 张图"
          f" · 并发 {args.concurrency}{' · 流式' if args.stream else ''}\n")

    t0 = time.time()
    if args.concurrency > 1:
        with cf.ThreadPoolExecutor(args.concurrency) as ex:
            results = list(ex.map(lambda p: ocr_one(p, args, key), images))
    else:
        results = [ocr_one(p, args, key) for p in images]
    total = time.time() - t0

    outdir = Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for r in results:
        name = r["path"].name
        if not r["ok"]:
            print(f"── {name}  失败 ({r['dt']:.1f}s)\n   {r['err']}\n")
            continue
        ok += 1
        ttfb = f" · 首字 {r['ttfb']:.1f}s" if r.get("ttfb") else ""
        print(f"── {name}  {r['dt']:.1f}s{ttfb} · {len(r['text'])} 字")
        if "<|" in r["text"] or "|>" in r["text"]:
            print("   ⚠ 输出里还有 <|token|> 残留，检查 OCR_HUB_STRIP_TOKENS")
        if outdir:
            f = outdir / (r["path"].stem + ".md")
            f.write_text(r["text"], encoding="utf-8")
            print(f"   → {f}")
        else:
            body = r["text"] if len(r["text"]) <= 1200 else r["text"][:1200] + "\n…(截断)"
            print("   " + body.replace("\n", "\n   "))
        print()

    times = [r["dt"] for r in results if r["ok"]]
    print(f"成功 {ok}/{len(results)} · 总耗时 {total:.1f}s", end="")
    if times:
        print(f" · 单张 最快 {min(times):.1f}s / 最慢 {max(times):.1f}s"
              f" / 平均 {sum(times)/len(times):.1f}s")
    else:
        print()
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
