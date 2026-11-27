# -*- coding: utf-8 -*-
"""
RAG 知识库 App「超脑工作搭子」—— 独立监测进程（防假死）
与 App 进程完全隔离，通过「进程级指标」监测（不依赖 App 的 HTTP——
App 在构建索引/切换模型时 HTTP 可能被 GIL 饿死，恰是最需要监测的时刻）：

  监测维度：
    1. App 进程存活（按 rag_app.py 命令行匹配，支持 18800/18801 等任意端口）
    2. HTTP 健康探测（/api/boot，状态与运行时长）
    3. 活性判定：CPU 时间增量 + app.log 日志增量
       （任一增长 → 正在工作中；双零 → 停滞可疑）
    4. 假死判定：进程存活但连续 STALL_ROUNDS 轮「双零停滞」

  动作：
    - 常态：写 monitor.log（--verbose 时同时打印）
    - 状态变化 / 假死：macOS 桌面通知 + 日志
    - 假死持续超过 RESTART_AFTER_STALL 轮且启用 --auto-restart：
      自动重启 App 进程（构建中断可通过增量构建安全恢复）

用法：
  .venv/bin/python app_monitor.py                 # 前台监测（打印+日志）
  .venv/bin/python app_monitor.py --quiet         # 静默，异常才通知
  .venv/bin/python app_monitor.py --auto-restart  # 假死超时自动重启 App
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil  # noqa: E402

KB_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_LOG = os.path.join(KB_ROOT, "app.log")
MONITOR_LOG = os.path.join(KB_ROOT, "monitor.log")
APP_LAUNCHER = os.path.join(KB_ROOT, "超脑工作搭子.app", "Contents", "MacOS", "RAGKB")

CHECK_INTERVAL = 10        # 监测轮询间隔（秒）
HTTP_TIMEOUT = 3           # HTTP 探测超时
STALL_ROUNDS = 18          # 连续停滞 18 轮（3分钟）判定假死
RESTART_AFTER_STALL = 30   # 停滞 30 轮（5分钟）触发自动重启（需 --auto-restart）
ALERT_DEDUP_SEC = 1800     # 同类告警 30 分钟去重（同技术说明书 10.1）

_state = {"pid": None, "cpu": 0.0, "log_lines": 0, "stall": 0,
          "http": "unknown", "last_alert": {}, "last_state": None}


def log(msg, quiet=False, notify_user=False):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if not quiet:
        print(line, flush=True)
    try:
        with open(MONITOR_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if notify_user:
        try:  # macOS 桌面通知
            subprocess.run(
                ["osascript", "-e", f'display notification "{msg}" with title "超脑工作搭子监测"'],
                timeout=5, capture_output=True, check=False)
        except Exception:  # noqa: BLE001
            pass


def alert_once(key, msg, severe=False, quiet=False):
    """告警去重：同类告警 30 分钟内不重复"""
    now = time.time()
    if now - _state["last_alert"].get(key, 0) < ALERT_DEDUP_SEC:
        return
    _state["last_alert"][key] = now
    log(f"[{'严重' if severe else '告警'}] {msg}", quiet=quiet, notify_user=True)


def find_app_process():
    """定位 App 主进程（v4.8 起经轻量入口 app_main.py 启动；兼容旧 rag_app.py 直接启动）"""
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.pid == me:
                continue
            cmdline = p.info.get("cmdline") or []
            if any(("rag_app.py" in str(c)) or ("app_main.py" in str(c)) for c in cmdline):
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def app_log_lines():
    try:
        with open(APP_LOG, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def http_boot_probe():
    """探测 App HTTP（18800 起，兼容备用端口）；返回 (ok, phase, elapsed)"""
    import requests
    for port in (18800, 18801, 18802, 18803):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/boot", timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                return True, data.get("phase", "?"), port
        except Exception:  # noqa: BLE001
            continue
    return False, None, None


def restart_app():
    """自动重启：终止假死进程 → 通过 .app 启动器拉起新进程"""
    p = find_app_process()
    if p:
        log(f"终止假死进程 pid={p.pid}", quiet=True, notify_user=False)
        try:
            p.terminate()
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        time.sleep(2)
    subprocess.Popen([APP_LAUNCHER], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _state["stall"] = 0


def check_once(quiet, auto_restart):
    """单轮监测：存活 → 活性 → HTTP → 状态判定与告警"""
    p = find_app_process()
    # ---------- 进程退出 ----------
    if p is None:
        if _state["pid"] is not None:
            alert_once("exit", "App 进程已退出（可能被关闭）", quiet=quiet)
        _state.update({"pid": None, "stall": 0, "http": "unknown", "last_state": None})
        log("App 未运行", quiet=True)
        return
    # ---------- 活性采样 ----------
    try:
        cpu = sum(p.cpu_times())
    except psutil.NoSuchProcess:
        return
    lines = app_log_lines()
    http_ok, phase, port = http_boot_probe()

    cpu_delta = cpu - _state["cpu"]
    log_delta = lines - _state["log_lines"]
    busy = cpu_delta >= 0.5 or log_delta > 0   # 任一增长即视为正在工作

    if http_ok:
        state = "serving"          # HTTP 正常响应
        stall = 0
    elif busy:
        state = "busy"             # 无HTTP但活跃（构建/模型加载/切换中）——健康
        stall = 0
    else:
        state = "stalled"
        stall = _state["stall"] + 1

    # 状态变化提示（busy↔serving↔stalled）
    if state != _state["last_state"]:
        desc = {"serving": f"服务正常（端口{port}，phase={phase}）",
                "busy": "工作繁忙中（构建/加载，HTTP暂不可用属正常）",
                "stalled": f"活动停滞（连续{stall}轮）"}[state]
        log(f"状态: {desc} pid={p.pid} cpuΔ={cpu_delta:.1f}s logΔ={log_delta}", quiet=quiet)
        _state["last_state"] = state
    # ---------- 假死判定与自动恢复 ----------
    if stall >= STALL_ROUNDS and stall % STALL_ROUNDS == 0:
        alert_once("stalled", f"疑似假死：进程存活但已连续停滞 {stall*CHECK_INTERVAL} 秒无活动",
                   severe=True, quiet=quiet)
    if auto_restart and stall >= RESTART_AFTER_STALL:
        alert_once("autorestart", f"假死持续超过 {RESTART_AFTER_STALL*CHECK_INTERVAL} 秒，执行自动重启",
                   severe=True, quiet=quiet)
        restart_app()

    _state.update({"pid": p.pid, "cpu": cpu, "log_lines": lines, "stall": stall, "http": state})


def main():
    parser = argparse.ArgumentParser(description="RAG App 独立监测进程（防假死）")
    parser.add_argument("--quiet", action="store_true", help="静默模式，异常才通知")
    parser.add_argument("--verbose", action="store_true", help="每轮输出（默认仅状态变化时输出）")
    parser.add_argument("--auto-restart", action="store_true",
                        help="假死持续超时后自动重启 App（默认只报警）")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL)
    args = parser.parse_args()
    quiet = args.quiet and not args.verbose
    log(f"监测进程启动（每{args.interval}秒一轮；假死阈值 {STALL_ROUNDS*args.interval}s；"
        f"自动重启 {'开启(停滞%ss)' % (RESTART_AFTER_STALL*args.interval) if args.auto_restart else '关闭'}）",
        quiet=quiet)
    while True:
        try:
            check_once(quiet=quiet, auto_restart=args.auto_restart)
        except KeyboardInterrupt:
            log("监测进程退出", quiet=quiet)
            break
        except Exception as e:  # noqa: BLE001
            log(f"监测循环异常: {e}", quiet=quiet)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
