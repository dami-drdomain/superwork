# -*- coding: utf-8 -*-
"""
超脑工作搭子 —— 自动收集模块（v5.2）

目标：本机其他软件（WPS、邮件、预览、浏览器下载等）「打开过」的文档，
自动收集进知识库 docs/自动收集/，无需用户手动搬运。

机制（macOS 原生能力，零第三方依赖，不 hook 任何 App）：
  1. Spotlight 最近使用查询（主通道，App 无关）：
     mdfind 'kMDItemLastUsedDate >= $time.iso(2026-11-02T00:00:00)'
     —— 任何软件通过 LaunchServices 打开文件，系统都会更新该文件的
        kMDItemLastUsedDate 元数据。「被 WPS / 邮件打开过」这一事实由
        系统统一记录，本模块只做查询。
     注意：查询串必须原样传给 mdfind（subprocess 列表传参，不经 shell），
     否则 $time 会被 shell 当变量展开导致查询失效。
  2. 邮件/IM 定点通道：邮件客户端（Apple Mail、网易邮箱大师）与 IM（微信、钉钉）
     把接收/查看的文件存在 ~/Library 下的固定目录（全局查询可见，但默认被
     ~/Library 排除规则拦截）。本模块对全局「最近使用」查询结果按目录包含
     关系判定归属：命中通道目录的标为对应渠道（邮件/微信/钉钉）并放行收集。
     邮件附件目录另做 mtime 兜底（刚落盘还未进 Spotlight 索引的新附件）；
     IM 目录只认打开记录（接收≠打开，实测微信目录大量文件从未打开过
     LastUsedDate 为空）。不用 mdfind -onlyin 定点查询——实测其对部分容器
     路径（如钉钉容器）会静默失效，而全局查询对 ~/Library 下文件均可见。
     新增客户端只需在 APP_CHANNELS 加一行 (目录, 标签, 是否mtime兜底)。

过滤：格式白名单（复用 rag_core.SUPPORTED_EXTS）、大小上限、系统目录/
      废纸篓/应用包/知识库自身排除、Office 锁文件（~$）等噪音排除。
收集：复制到 docs/自动收集/YYYY-MM/；同名同大小视为已收集（跳过），
      同名不同大小（用户改过内容）追加时间戳副本。
入库：收集目录在 docs/ 下，增量构建时自然被 scan_files 扫描。

配置：rag-kb/autocollect_config.json（enabled / interval_min / max_mb / lookback_days）
状态：docs/.rag_db_embed/.autocollect_state.json（last_scan、累计数、最近收集清单）

本模块仅依赖标准库 + rag_core 常量，不加载 torch 等重型依赖，
可被 rag_app.py 模块级安全导入。独立测试：python rag_autocollect.py
"""
import json
import os
import shutil
import stat as stat_mod
import subprocess
import threading
import time
from datetime import datetime, timedelta

from rag_core import DOCS_DIR, KB_ROOT, SUPPORTED_EXTS, is_noise_file  # noqa: F401

DB_DIR = os.path.join(DOCS_DIR, ".rag_db_embed")
CONFIG_PATH = os.path.join(KB_ROOT, "autocollect_config.json")
STATE_PATH = os.path.join(DB_DIR, ".autocollect_state.json")
COLLECT_ROOT = os.path.join(DOCS_DIR, "自动收集")

DEFAULT_CONFIG = {
    "enabled": True,
    "interval_min": 10,    # 定时扫描间隔（分钟）
    "max_mb": 20,          # 单文件大小上限（MB，与知识库索引上限对齐；V1.6.3 由 10 提升）
    "max_mb_ppt": 200,     # V1.7.1：ppt/pptx 专用上限（MB，与 rag_core.MAX_FILE_SIZE_PPT 对齐）
    "lookback_days": 2,    # 「最近使用」回溯窗口（天），覆盖 App 没开机的时段
}

# 定点通道：邮件/IM 客户端的可信目录（全局查询命中这些目录的文件标为对应渠道，免 ~/Library 排除）。
# 元组：(目录, 渠道标签, 是否允许 mtime 兜底)
#  · 邮件目录：附件在「查看/预览」时才落盘，mtime≈查看时间 → 允许 mtime 兜底
#  · IM 目录（微信/钉钉）：接收即落盘但未必打开过（实测大量文件 LastUsedDate 为空），
#    只凭「最近使用时间」判定，不做 mtime 兜底，避免把聊天下发但从未打开的文档全部收进来
APP_CHANNELS = [
    (os.path.expanduser("~/Library/Containers/com.apple.mail/Data/Library/Mail Downloads"), "邮件", True),
    (os.path.expanduser("~/Library/Containers/com.netease.macmail/Data/Library/Application Support/view"), "邮件", True),
    # 微信 4.x（本机实测：xwechat_files/<账号>/msg/file/<年-月>/）＋3.x 旧路径兼容
    (os.path.expanduser("~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"), "微信", False),
    (os.path.expanduser("~/Documents/xwechat_files"), "微信", False),
    # 钉钉沙盒容器：其 Data/Downloads 是指向 ~/Downloads 的符号链接（钉钉默认把
    # 下载存到用户下载目录），符号链接通道会被自动跳过，那些文件打开后由
    # Spotlight 通道正确覆盖；此处保留真实目录 + 旧版容器 ID 兼容
    (os.path.expanduser("~/Library/Containers/5ZSL2CJU2T.com.dingtalk.mac/Data/Documents"), "钉钉", False),
    (os.path.expanduser("~/Library/Group Containers/5ZSL2CJU2T.com.dingtalk.mac"), "钉钉", False),
    (os.path.expanduser("~/Library/Containers/com.alibaba.DingTalk/Data/Documents"), "钉钉", False),
]

# Spotlight 结果排除区（realpath 归一化后比对；命中定点通道目录的文件不受此限）
_EXCLUDE_PREFIXES = [
    "/System", "/private/var/db", "/Library", "/Applications",
    os.path.expanduser("~/Library"),
    os.path.expanduser("~/.Trash"),
    os.path.realpath(KB_ROOT),   # 知识库自身（避免收集已在库内/已收集过的文件）
]

_scan_lock = threading.Lock()   # 防止定时扫描与手动扫描并发复制


# ---------- 配置与状态 ----------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except (OSError, json.JSONDecodeError):
        pass
    return {"last_scan": None, "collected_total": 0, "recent": []}


def _save_state(st: dict):
    os.makedirs(DB_DIR, exist_ok=True)
    st["recent"] = st.get("recent", [])[:50]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------- 候选文件发现 ----------
def _mdfind(*args) -> list:
    """调用 mdfind（列表传参绕开 shell，保证 $time 变量原样送达）"""
    try:
        r = subprocess.run(["mdfind", *args], capture_output=True, text=True, timeout=30)
        return [p for p in r.stdout.splitlines() if p.strip()]
    except Exception:  # noqa: BLE001
        return []


def spotlight_recent(lookback_days: int) -> list:
    """最近被任何软件打开过的文件（LaunchServices 使用记录）"""
    start = datetime.now() - timedelta(days=max(1, int(lookback_days)))
    query = "kMDItemLastUsedDate >= $time.iso(%s)" % start.strftime("%Y-%m-%dT%H:%M:%S")
    paths = _mdfind(query)
    if not paths:   # 兜底：iso 语法万一失效则退回 $time.today（仅覆盖今天）
        paths = _mdfind("kMDItemLastUsedDate >= $time.today")
    return paths


def _channel_dirs() -> list:
    """有效定点通道目录（realpath 归一化）。跳过不存在与符号链接目录——
    如钉钉 Data/Downloads→~/Downloads 的符号链接会把整个下载目录错误标为钉钉。"""
    out = []
    for d, label, _fb in APP_CHANNELS:
        if os.path.isdir(d) and not os.path.islink(d):
            out.append((os.path.realpath(d), label))
    return out


def _channel_of(p: str, channel_dirs: list) -> str | None:
    """按目录包含关系判断文件属于哪个定点通道"""
    rp = os.path.realpath(p)
    for d, label in channel_dirs:
        if rp == d or rp.startswith(d + os.sep):
            return label
    return None


def mail_fresh(lookback_days: int) -> list:
    """邮件附件目录的 mtime 兜底：刚落盘、尚未进 Spotlight 索引的新附件，返回 (路径, 渠道)。
    仅邮件目录启用（附件在查看时才落盘，mtime≈查看时间）；
    IM 目录不做此兑底——接收≠打开，mtime 兑底会把聊天下发但从未打开的文档全部收进来。"""
    cutoff = time.time() - max(1, int(lookback_days)) * 86400
    out, seen = [], set()
    for d, label, mtime_fallback in APP_CHANNELS:
        if not mtime_fallback or not os.path.isdir(d) or os.path.islink(d):
            continue
        for root, _dirs, names in os.walk(d):
            for name in names:
                p = os.path.join(root, name)
                if p in seen:
                    continue
                try:
                    if os.path.getmtime(p) >= cutoff:
                        seen.add(p)
                        out.append((p, label))
                except OSError:
                    continue
    return out


# ---------- 过滤 ----------
def _excluded(p: str) -> bool:
    rp = os.path.realpath(p)
    if "/.app/Contents" in rp or "/.Trash/" in rp:
        return True
    for pre in _EXCLUDE_PREFIXES:
        if rp == pre or rp.startswith(pre + os.sep):
            return True
    return False


def _accept(p: str, max_bytes: int, max_bytes_ppt: int | None = None) -> bool:
    name = os.path.basename(p)
    ext = os.path.splitext(name)[1].lower()
    if ext not in SUPPORTED_EXTS:
        return False
    if ext in (".ppt", ".pptx") and max_bytes_ppt:
        max_bytes = max_bytes_ppt   # v5.3.4：PPT 体积主要是媒体资源，放宽上限
    if name.startswith(".") or name.startswith("~$") or is_noise_file(name, name):
        return False
    try:
        st = os.stat(p)
    except OSError:
        return False
    if not stat_mod.S_ISREG(st.st_mode):
        return False
    return 0 < st.st_size <= max_bytes


# ---------- 复制入库 ----------
def _copy_into_docs(p: str) -> str | None:
    """复制到 docs/自动收集/YYYY-MM/；同名同大小返回 None（已收集过）"""
    name = os.path.basename(p)
    sub = datetime.now().strftime("%Y-%m")
    dest_dir = os.path.join(COLLECT_ROOT, sub)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest):
        if os.path.getsize(dest) == os.path.getsize(p):
            return None
        base, ext = os.path.splitext(name)
        dest = os.path.join(dest_dir, f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}")
    shutil.copy2(p, dest)
    return dest


# ---------- 对外主入口 ----------
def scan_once(log=None) -> dict:
    """执行一次扫描收集（线程安全，重入直接返回 busy）。不触发构建，由调用方决定。"""
    if not _scan_lock.acquire(blocking=False):
        return {"error": "已有扫描进行中，请稍候"}
    t0 = time.time()
    try:
        cfg = load_config()
        max_bytes = int(cfg["max_mb"]) * 1024 * 1024
        max_bytes_ppt = int(cfg.get("max_mb_ppt") or 100) * 1024 * 1024
        lookback = int(cfg["lookback_days"])

        # 全局「最近使用」查询按目录包含关系判定渠道（邮件/微信/钉钉等定点通道免 ~/Library 排除）；
        # 邮件附件目录另做 mtime 兑底；realpath 去重（邮件兑底优先）。
        # 注：不用 mdfind -onlyin 定点查询——实测它对部分容器路径（如钉钉）会静默失效，
        # 而全局查询对 ~/Library 下文件同样可见（微信/钉钉/邮件三目录均实测命中）。
        channel_dirs = _channel_dirs()
        entries = mail_fresh(lookback)
        for p in spotlight_recent(lookback):
            entries.append((p, _channel_of(p, channel_dirs) or "最近使用"))
        seen, cands = set(), []
        for p, label in entries:
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            seen.add(rp)
            cands.append((p, label))

        collected, skipped_existing, filtered = [], 0, 0
        for p, label in cands[:2000]:
            if not _accept(p, max_bytes, max_bytes_ppt):
                filtered += 1
                continue
            if label == "最近使用" and _excluded(p):
                filtered += 1
                continue
            try:
                dest = _copy_into_docs(p)
            except OSError as e:
                filtered += 1
                if log:
                    log(f"[自动收集] 复制失败 {p}: {e}")
                continue
            if dest is None:
                skipped_existing += 1
                continue
            collected.append({
                "name": os.path.basename(dest),
                "source": p,
                "dest": os.path.relpath(dest, KB_ROOT),
                "size_kb": round(os.path.getsize(dest) / 1024, 1),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "channel": label,
            })
            if log:
                month = datetime.now().strftime("%Y-%m")
                log(f"[自动收集] 收集[{label}] {os.path.basename(p)} → 自动收集/{month}/")

        st = load_state()
        st["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["collected_total"] = st.get("collected_total", 0) + len(collected)
        st["recent"] = collected + st.get("recent", [])
        _save_state(st)
        return {
            "candidates": len(cands),
            "collected": collected,
            "skipped_existing": skipped_existing,
            "filtered": filtered,
            "last_scan": st["last_scan"],
            "elapsed": round(time.time() - t0, 2),
        }
    finally:
        _scan_lock.release()


if __name__ == "__main__":
    result = scan_once(log=print)
    print(json.dumps({k: v for k, v in result.items() if k != "collected"},
                     ensure_ascii=False, indent=2))
    print(f"本次收集 {len(result.get('collected', []))} 个文件")
