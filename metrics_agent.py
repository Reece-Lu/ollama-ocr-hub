"""macOS 宿主机指标服务。

这个进程必须原生运行在 Mac 上，不能放进 Docker，否则读到的是 Linux
虚拟机的资源。默认只监听 127.0.0.1，供 Docker Desktop 里的面板通过
host.docker.internal 读取。
"""

import os
import platform
import re
import shutil
import subprocess
import threading
import time
from ctypes import CDLL, POINTER, byref, c_int, c_uint, c_void_p

from fastapi import FastAPI

app = FastAPI(title="OCR Hub Mac Metrics", docs_url=None, redoc_url=None)

_GPU_FIELDS = {
    "device": re.compile(r'"Device Utilization %"=(\d+)'),
    "renderer": re.compile(r'"Renderer Utilization %"=(\d+)'),
    "tiler": re.compile(r'"Tiler Utilization %"=(\d+)'),
}

_CPU_TICKS = c_uint * 4
_CPU_LOAD_INFO = 3
_cpu_lock = threading.Lock()
_last_cpu_ticks = None


def _read_cpu_ticks():
    if platform.system() != "Darwin":
        return None
    lib = CDLL("/usr/lib/libSystem.B.dylib")
    lib.mach_host_self.restype = c_uint
    lib.host_statistics.argtypes = [c_uint, c_int, c_void_p, POINTER(c_uint)]
    lib.host_statistics.restype = c_int
    ticks = _CPU_TICKS()
    count = c_uint(len(ticks))
    result = lib.host_statistics(
        lib.mach_host_self(), _CPU_LOAD_INFO, byref(ticks), byref(count)
    )
    return tuple(ticks) if result == 0 else None


def cpu_percent() -> float:
    """用 Mach host_statistics 的 CPU tick 差值计算整机使用率。"""
    global _last_cpu_ticks
    current = _read_cpu_ticks()
    if current is None:
        return 0.0
    with _cpu_lock:
        previous, _last_cpu_ticks = _last_cpu_ticks, current
    if previous is None:
        return 0.0
    deltas = [max(0, now - old) for now, old in zip(current, previous)]
    total = sum(deltas)
    idle = deltas[2]
    return round((total - idle) / total * 100, 1) if total else 0.0


_last_cpu_ticks = _read_cpu_ticks()


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    return result.stdout


def memory_metrics() -> dict:
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = os.sysconf("SC_PHYS_PAGES") * page_size
    output = _run([shutil.which("vm_stat") or "/usr/bin/vm_stat"])
    pages = {
        key: int(value)
        for key, value in re.findall(r'^([^:]+):\s+(\d+)\.', output, re.M)
    }
    available_pages = (
        pages.get("Pages free", 0)
        + pages.get("Pages inactive", 0)
        + pages.get("Pages speculative", 0)
    )
    available = min(total, available_pages * page_size)
    used = max(0, total - available)
    return {
        "total": total,
        "used": used,
        "available": available,
        "compressed": pages.get("Pages occupied by compressor", 0) * page_size,
        "percent": round(used / total * 100, 1) if total else 0.0,
    }


def swap_metrics() -> dict:
    sysctl = shutil.which("sysctl") or "/usr/sbin/sysctl"
    try:
        output = _run([sysctl, "-n", "vm.swapusage"])
        values = {
            key.lower(): float(value)
            for key, value in re.findall(
                r"(total|used|free)\s*=\s*([0-9.]+)M", output, re.I
            )
        }
        total = int(values.get("total", 0) * 1024 * 1024)
        used = int(values.get("used", 0) * 1024 * 1024)
        return {
            "available": True,
            "total": total,
            "used": used,
            "percent": round(used / total * 100, 1) if total else 0.0,
        }
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "total": 0, "used": 0, "percent": 0.0}


def gpu_metrics() -> dict:
    if platform.system() != "Darwin":
        return {"available": False, "reason": "仅支持 macOS"}

    ioreg = shutil.which("ioreg") or "/usr/sbin/ioreg"
    try:
        output = _run([ioreg, "-r", "-c", "AGXAccelerator", "-d", "1"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc)[:160]}

    values = {}
    for name, pattern in _GPU_FIELDS.items():
        match = pattern.search(output)
        values[name] = float(match.group(1)) if match else None
    if values["device"] is None:
        return {"available": False, "reason": "未找到 Apple GPU 使用率"}
    return {"available": True, "source": "ioreg", **values}


def collect_metrics() -> dict:
    load1, load5, load15 = os.getloadavg()
    return {
        "timestamp": time.time(),
        "host": platform.node(),
        "cpu": {
            "percent": cpu_percent(),
            "logical_cores": os.cpu_count(),
            "load_1": load1,
            "load_5": load5,
            "load_15": load15,
        },
        "memory": memory_metrics(),
        "swap": swap_metrics(),
        "gpu": gpu_metrics(),
    }


@app.get("/metrics")
def metrics():
    return collect_metrics()


@app.get("/health")
def health():
    return {"ok": True, "platform": platform.system()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("OCR_HUB_METRICS_BIND", "127.0.0.1"),
        port=int(os.environ.get("OCR_HUB_METRICS_PORT", "9105")),
    )
