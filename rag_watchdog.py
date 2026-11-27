# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 守护进程（Watchdog）
按《RAG知识库系统关键技术说明书 v3.0》10.1 实现：
  - 30秒健康探针 / 异常自动重启（3次/小时上限）
  - 重启流程：杀残留旧服务进程 → 后台启动新服务 → 每10秒探测 /health，最多等待150秒
  - 磁盘告警（剩余<10%普通，<5%严重）/ 内存告警（>80%普通，>90%严重）
  - 告警去重窗口30分钟
用法：
  python rag_watchdog.py            # 常驻模式，30秒轮询
  python rag_watchdog.py --once     # 单次检查→重启→退出，适合定时任务
  python rag_watchdog.py --once --quiet  # 静默模式，正常无输出，异常才输出+通知
"""
import argparse
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil  # noqa: E402
import requests  # noqa: E402

from rag_core import SERVER_HOST, SERVER_PORT  # noqa: E402

HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/health"
CHECK_INTERVAL = 30          # 检查间隔
MAX_RESTARTS_PER_HOUR = 3    # 最大重启频率
RESTART_WAIT_TIMEOUT = 150   # 重启等待超时（模型加载约50秒）
RESTART_PROBE_INTERVAL = 10  # 每10秒探测
DISK_WARN, DISK_CRITICAL = 10, 5      # 磁盘告警阈值（剩余%）
MEM_WARN, MEM_CRITICAL = 80, 90        # 内存告警阈值（占用%）
ALERT_DEDUP_WINDOW = 30 * 60          # 告警去重窗口30分钟

KB_ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(KB_ROOT, "rag_server.py")
LOG_FILE = os.path.join(KB_ROOT, "watchdog.log")
_last_alerts = {}          # 告警去重
_restart_times = deque()  # 最近一小时的重启时间


def log(msg, quiet=False, notify=False):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if not quiet:
        print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if notify:
        try:  # macOS 桌面通知（Windows 为 Toast，此处平台兼容处理）
            subprocess.run(
                ["osascript", "-e", f'display notification "{msg}" with title "RAG守护进程"'],
                timeout=5, capture_output=True, check=False,
            )
        except Exception:  # noqa: BLE001
            pass


def alert(key, msg, severe=False, quiet=False):
    """告警去重：同一告警30分钟内不重复记录"""
    now = time.time()
    if now - _last_alerts.get(key, 0) < ALERT_DEDUP_WINDOW:
        return
    _last_alerts[key] = now
    log(f"[{'严重' if severe else '告警'}] {msg}", quiet=quiet, notify=severe)


def service_healthy(timeout=5.0):
    try:
        r = requests.get(HEALTH_URL, timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def kill_stale_server():
    """杀死残留的旧服务进程（通过进程列表匹配脚本名）"""
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if proc.pid == me or not cmdline:
                continue
            if any("rag_server.py" in str(c) for c in cmdline):
                log(f"终止残留服务进程 pid={proc.pid}")
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def start_server():
    """后台启动新服务进程（无窗口模式）"""
    env = dict(os.environ)
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    log_file = open(os.path.join(KB_ROOT, "server.log"), "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, SERVER_SCRIPT],
        stdout=log_file, stderr=subprocess.STDOUT, env=env,
        cwd=KB_ROOT, start_new_session=True,
    )


def restart_server(quiet=False):
    """重启流程（10.1）：杀旧 → 启新 → 探测就绪（最多150秒）"""
    now = time.time()
    while _restart_times and now - _restart_times[0] > 3600:
        _restart_times.popleft()
    if len(_restart_times) >= MAX_RESTARTS_PER_HOUR:
        alert("restart_limit", "一小时内重启已达3次上限，需人工介入", severe=True, quiet=quiet)
        return False
    _restart_times.append(now)

    log("服务异常，开始自动重启 ...", quiet=quiet)
    kill_stale_server()
    time.sleep(2)
    start_server()
    waited = 0
    while waited < RESTART_WAIT_TIMEOUT:
        time.sleep(RESTART_PROBE_INTERVAL)
        waited += RESTART_PROBE_INTERVAL
        if service_healthy():
            log("服务重启成功，/health 就绪", quiet=quiet)
            return True
    alert("restart_timeout", "服务重启超时（150秒未就绪），需人工介入", severe=True, quiet=quiet)
    return False


def check_resources(quiet=False):
    """磁盘/内存监控与告警（10.1）"""
    usage = psutil.disk_usage(KB_ROOT)
    disk_pct = usage.percent
    free_pct = 100 - disk_pct
    if free_pct < DISK_CRITICAL:
        alert("disk", f"磁盘剩余空间严重不足: {free_pct:.1f}% (<{DISK_CRITICAL}%)", severe=True, quiet=quiet)
    elif free_pct < DISK_WARN:
        alert("disk", f"磁盘剩余空间不足: {free_pct:.1f}%", quiet=quiet)

    mem_pct = psutil.virtual_memory().percent
    if mem_pct > MEM_CRITICAL:
        alert("mem", f"系统内存占用严重: {mem_pct:.1f}% (>{MEM_CRITICAL}%)", severe=True, quiet=quiet)
    elif mem_pct > MEM_WARN:
        alert("mem", f"系统内存占用偏高: {mem_pct:.1f}%", quiet=quiet)


def once(quiet=False):
    """单次检查：健康检查 → 异常时重启 → 资源监控 → 退出"""
    ok = service_healthy()
    if not ok:
        restart_server(quiet=quiet)
    else:
        if not quiet:
            log("服务运行正常")
    check_resources(quiet=quiet)


def force_restart(quiet=False):
    """强制重启服务（夜间构建后加载新索引用），复用标准重启流程"""
    log("强制重启服务（加载最新索引）...", quiet=quiet)
    kill_stale_server()
    time.sleep(2)
    start_server()
    waited = 0
    while waited < RESTART_WAIT_TIMEOUT:
        time.sleep(RESTART_PROBE_INTERVAL)
        waited += RESTART_PROBE_INTERVAL
        if service_healthy():
            log("服务重启成功，/health 就绪", quiet=quiet)
            check_resources(quiet=quiet)
            return True
    alert("restart_timeout", "服务重启超时（150秒未就绪），需人工介入", severe=True, quiet=quiet)
    return False


def daemon(quiet=False):
    """常驻模式：30秒轮询"""
    log(f"守护进程启动（每{CHECK_INTERVAL}秒探针，端口{SERVER_PORT}）")
    while True:
        try:
            if not service_healthy():
                restart_server(quiet=quiet)
            check_resources(quiet=quiet)
        except KeyboardInterrupt:
            log("守护进程退出")
            break
        except Exception as e:  # noqa: BLE001
            log(f"守护循环异常: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 服务守护进程")
    parser.add_argument("--once", action="store_true", help="单次检查模式（适合定时任务）")
    parser.add_argument("--restart", action="store_true", help="强制重启服务（夜间构建后加载新索引）")
    parser.add_argument("--quiet", action="store_true", help="静默模式，正常无输出")
    args = parser.parse_args()
    if args.restart:
        force_restart(quiet=args.quiet)
    elif args.once:
        once(quiet=args.quiet)
    else:
        daemon(quiet=args.quiet)
