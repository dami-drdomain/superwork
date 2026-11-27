# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 备份恢复工具
按《RAG知识库系统关键技术说明书 v3.0》10.3 实现：
  - 增量快照备份（仅复制与上次备份不同的文件），保留最近7天
  - 全量备份 / 快照列表 / 从快照恢复 / 过期清理
  - 恢复安全机制：服务运行中拒绝恢复；恢复前当前数据移至 _before_restore
用法：
  python rag_backup.py backup          # 增量备份
  python rag_backup.py backup --full   # 全量备份
  python rag_backup.py list           # 列出所有快照
  python rag_backup.py restore --snapshot <名称>
  python rag_backup.py cleanup        # 清理7天前的过期备份
"""
import argparse
import os
import shutil
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil  # noqa: E402
import requests  # noqa: E402

from rag_core import DB_DIR, SERVER_HOST, SERVER_PORT  # noqa: E402

KB_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_ROOT = os.path.join(KB_ROOT, "backups")
RETENTION_DAYS = 7
HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/health"


def service_running():
    try:
        return requests.get(HEALTH_URL, timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        # 兜底：按进程列表确认
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = proc.info.get("cmdline") or []
            if any("rag_server.py" in str(c) for c in cmdline):
                return True
        return False


def snapshot_name():
    return f"snapshot_{datetime.now():%Y%m%d_%H%M%S}"


def _copy_incremental(src_dir, dest_dir, base_dir):
    """增量复制 src_dir → dest_dir：与基准快照同指纹（大小+mtime）的文件从基准复制，其余直接复制
    返回 (copied, skipped)；base_dir 为 None 时全部直接复制（全量）"""
    copied = skipped = 0
    for root, _dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        out_dir = os.path.join(dest_dir, rel) if rel != "." else dest_dir
        os.makedirs(out_dir, exist_ok=True)
        for fname in files:
            src = os.path.join(root, fname)
            out = os.path.join(out_dir, fname)
            if base_dir:
                ref = os.path.join(base_dir, rel, fname)
                if os.path.exists(ref):
                    s_stat, r_stat = os.stat(src), os.stat(ref)
                    if s_stat.st_size == r_stat.st_size and int(s_stat.st_mtime) == int(r_stat.st_mtime):
                        shutil.copy2(ref, out)  # 与上次相同，从基准快照复制
                        skipped += 1
                        continue
            shutil.copy2(src, out)
            copied += 1
    return copied, skipped


def backup(full=False):
    name = snapshot_name()
    dest = os.path.join(BACKUP_ROOT, name)
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    t0 = time.time()
    # 找最近一次快照作为增量基准
    base = None
    if not full:
        snaps = sorted(s for s in os.listdir(BACKUP_ROOT) if s.startswith("snapshot_"))
        if snaps:
            base = os.path.join(BACKUP_ROOT, snaps[-1])
    if base and os.path.abspath(base) == os.path.abspath(dest):
        base = None   # 同秒重复备份：快照名撞车时基准指向自身，退化为全量复制防 SameFileError
    parts = []
    if os.path.isdir(DB_DIR):
        parts.append((DB_DIR, ""))
    else:
        print(f"[!] 索引目录不存在（本快照跳过索引）: {DB_DIR}")
    if not parts:
        print("[x] 无可备份内容，已取消")
        return
    copied = skipped = 0
    for src_dir, sub in parts:
        dest_dir = os.path.join(dest, sub) if sub else dest
        c, s = _copy_incremental(src_dir, dest_dir, os.path.join(base, sub) if base else None)
        copied += c
        skipped += s
    print(f"[✓] 备份完成: {name}（新增/变更 {copied} 文件，未变化 {skipped}），"
          f"耗时 {time.time()-t0:.1f}s → {dest}")


def list_snapshots():
    if not os.path.isdir(BACKUP_ROOT):
        print("暂无备份")
        return
    snaps = sorted(s for s in os.listdir(BACKUP_ROOT) if s.startswith("snapshot_"))
    if not snaps:
        print("暂无备份")
        return
    for s in snaps:
        size = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _d, fs in os.walk(os.path.join(BACKUP_ROOT, s)) for f in fs
        )
        print(f"{s}  {size/1024/1024:.1f} MB")


def restore(snapshot):
    src = os.path.join(BACKUP_ROOT, snapshot)
    if not os.path.isdir(src):
        print(f"[!] 快照不存在: {snapshot}")
        return
    if service_running():
        print("[x] RAG 服务正在运行，请先停止服务再恢复（保护数据一致性）")
        return
    # 恢复安全机制：当前数据先移至 _before_restore 作为安全网
    if os.path.isdir(DB_DIR):
        safety = os.path.join(KB_ROOT, "_before_restore")
        if os.path.isdir(safety):
            shutil.rmtree(safety)
        shutil.move(DB_DIR, safety)
        print(f"[i] 当前索引已移至 {safety}（安全网）")
    # 旧版快照可能含已下线的 wiki/ 子目录，恢复时排除（仅恢复索引）
    shutil.copytree(src, DB_DIR, ignore=shutil.ignore_patterns("wiki"))
    print(f"[✓] 已从快照恢复索引: {snapshot} → {DB_DIR}")


def cleanup():
    if not os.path.isdir(BACKUP_ROOT):
        print("暂无备份")
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for s in os.listdir(BACKUP_ROOT):
        p = os.path.join(BACKUP_ROOT, s)
        if s.startswith("snapshot_") and os.path.getmtime(p) < cutoff:
            shutil.rmtree(p)
            removed += 1
    print(f"[✓] 清理完成：删除 {removed} 个超过 {RETENTION_DAYS} 天的过期备份")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 索引备份恢复工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_b = sub.add_parser("backup", help="增量备份（--full 全量）")
    p_b.add_argument("--full", action="store_true")
    sub.add_parser("list", help="列出所有备份快照")
    p_r = sub.add_parser("restore", help="从快照恢复")
    p_r.add_argument("--snapshot", required=True)
    sub.add_parser("cleanup", help="清理7天前的过期备份")

    args = parser.parse_args()
    if args.cmd == "backup":
        backup(full=getattr(args, "full", False))
    elif args.cmd == "list":
        list_snapshots()
    elif args.cmd == "restore":
        restore(args.snapshot)
    elif args.cmd == "cleanup":
        cleanup()
