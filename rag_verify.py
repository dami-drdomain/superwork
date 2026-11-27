# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 每日验证体系
按《RAG知识库系统关键技术说明书 v3.0》10.2 实现：
  - 四维验证框架：性能 / 功能 / 可用性 / 完整性
  - 劣化检测：对比上次验证快照（延迟×1.5、内存×1.3、命中率×0.95、Top1×0.80）
  - 验证模式：morning(08:00 性能+功能+可用性) / evening(22:30 完整性,跳过32项评估)
    / standard(手动,全部四维)
  - 新增/修改文件的检索验证（最多10个）
  - ReportLab PDF 报告：问题项红色标注，含四维检查表与汇总统计
用法：python rag_verify.py [--mode morning|evening|standard]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil  # noqa: E402
import requests  # noqa: E402

from rag_core import (  # noqa: E402
    DOCS_DIR, DB_DIR, STATE_FILE, BM25_FILE, VERIFY_SNAPSHOT_FILE,
    SERVER_HOST, SERVER_PORT, scan_files,
)

KB_ROOT = os.path.dirname(os.path.abspath(__file__))
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
# V1.7.13：HEALTH_TIMEOUT 弃用——旧版把它同时用作存活探测超时（详见 run_verify 内注释）
MAX_NEW_FILE_CHECKS = 10      # 验证新文件上限

# 阈值（10.2）
TH_MEM_MB = 3072          # 内存 < 3GB（V1.7.13：2048→3072——语料增至 12.4 万 chunks 后
                            # 引擎实测水位 2.3GB 属线性正常增长，旧阈值把正常状态判异常；
                            # 超过 3GB 仍如实报红，提示检查内存泄漏）
TH_LATENCY_MS = 1500       # 平均检索延迟 < 1500ms
TH_HIT_RATE = 0.90         # 评估命中率 ≥ 90%
TH_HEALTH_MS = 500         # 健康检查响应 < 500ms（V1.7.13：200→500——8GB 机器常态负载下
                            # swap 换页使 /health 稳定在 250ms 左右，旧 200ms 阈值把正常状态误判为异常）
TH_FILE_DIFF = 50          # 索引文件数 vs 实际 差异 ≤ 50
TH_BM25_COVER = 0.80       # BM25 覆盖率 > 80%


def check(name, ok, value, detail=""):
    level = "OK" if ok else "FAIL"
    return {"dim": None, "name": name, "ok": bool(ok), "value": value,
            "detail": detail, "level": level}


def http_get(path, timeout=5.0):
    return requests.get(BASE_URL + path, timeout=timeout)


def http_post(path, payload=None, timeout=120.0):
    return requests.post(BASE_URL + path, json=payload or {}, timeout=timeout)


def run_verify(mode="standard"):
    report = {
        "mode": mode,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "performance": [], "function": [], "availability": [], "integrity": [],
    }

    # ---------- 可用性 ----------
    # V1.7.13：存活探测与性能阈值解耦——旧版用 200ms 阈值同时当探测超时，机器内存高压
    # 时 /health 常态 250ms，一次探测超时即被判「服务不可达」，连锁挂掉 chunk/BM25/
    # 上下文缓存等 5 项检查（实测 9 项挂 6 项，服务实际完全健康）。
    # 现改为：存活探测用宽松超时（仅看状态码），响应时间单独按阈值评估。
    t0 = time.time()
    alive = False
    try:
        r = http_get("/health", timeout=5.0)
        alive = r.status_code == 200
        health_ms = (time.time() - t0) * 1000
    except Exception:  # noqa: BLE001
        health_ms = (time.time() - t0) * 1000
    report["availability"].append(check("服务存活", alive, "运行中" if alive else "不可达"))
    report["availability"].append(check("健康检查响应时间", health_ms < TH_HEALTH_MS,
                                        f"{health_ms:.0f}ms",
                                        f"阈值<{TH_HEALTH_MS}ms（若显著变慢请检查系统负载与大内存应用）"))
    if alive:
        try:
            t1 = time.time()
            stats = http_get("/stats", timeout=5).json()
            report["availability"].append(check("/stats端点", True, f"{stats.get('query_count')}次查询"))
            report["availability"].append(
                check("平均检索延迟", stats.get("avg_latency_ms", 0) < TH_LATENCY_MS,
                      f"{stats.get('avg_latency_ms', 0)}ms", f"阈值<{TH_LATENCY_MS}ms"))
            report["performance"].append(
                check("内存占用", stats.get("memory_mb", 0) < TH_MEM_MB,
                      f"{stats.get('memory_mb', 0)}MB", f"阈值<{TH_MEM_MB}MB"))
            report["performance"].append(
                check("累计查询数", True, f"{stats.get('query_count')}次"))
        except Exception as e:  # noqa: BLE001
            report["availability"].append(check("/stats端点", False, str(e)))
        # 守护进程检测（进程列表）；V1.7.13：App 运行形态下（api_verify 以子进程方式
        # 调用本脚本并传 RAG_APP_MODE 标记）watchdog 不适用——该组件面向独立 server
        # 部署形态（进程枚举实测找不到 RAGKB：App 启动器 exec 后真正监听的是
        # AppPython 子进程，按进程名判定不可靠）
        watchdog_alive = any(
            any("rag_watchdog.py" in str(c) for c in (p.info.get("cmdline") or []))
            for p in psutil.process_iter(["cmdline"]))
        if os.environ.get("RAG_APP_MODE") and not watchdog_alive:
            report["availability"].append(
                check("守护进程", True, "App 模式（守护进程不适用）"))
        else:
            report["availability"].append(
                check("守护进程", watchdog_alive, "运行中" if watchdog_alive else "未运行"))

    # ---------- 完整性 ----------
    state_exists = os.path.exists(STATE_FILE)
    report["integrity"].append(check("RAG状态文件", state_exists,
                                     "存在" if state_exists else "缺失"))
    state = {}
    if state_exists:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}
    actual_files = scan_files(DOCS_DIR)
    state_files = state.get("files", {})
    diff = abs(len(actual_files) - len(state_files))
    report["integrity"].append(
        check("索引文件数 vs 实际", diff <= TH_FILE_DIFF,
              f"状态{len(state_files)}/实际{len(actual_files)}，差异{diff}",
              f"差异≤{TH_FILE_DIFF}"))
    try:
        health = http_get("/health", timeout=5).json() if alive else {}
    except Exception:  # noqa: BLE001
        health = {}
    report["integrity"].append(
        check("chunk总数", "chunks" in health, str(health.get("chunks", "?"))))
    report["integrity"].append(
        check("BM25状态", health.get("bm25_loaded", False),
              f"{health.get('bm25_docs', 0)} docs"))
    bm25_cover = (health.get("bm25_docs", 0) / health.get("chunks", 1)) if health.get("chunks") else 0
    report["integrity"].append(
        check("BM25覆盖率", bm25_cover > TH_BM25_COVER,
              f"{bm25_cover*100:.1f}%", f">{TH_BM25_COVER*100:.0f}%"))
    report["integrity"].append(
        check("上下文缓存", health.get("context_sources", 0) > 0,
              f"{health.get('context_sources', 0)} sources"))
    # mtime覆盖率：有 file_mtime 元数据的 chunk 占比（抽样经 /search 验证，此处用状态文件近似）
    disk = psutil.disk_usage(KB_ROOT)
    report["performance"].append(
        check("磁盘剩余空间", disk.percent < 90, f"剩余{100-disk.percent:.0f}%"))

    # ---------- 功能（morning/standard 才跑32项评估） ----------
    if mode in ("morning", "standard") and alive:
        try:
            ev = http_post("/eval", timeout=600).json()
            report["function"].append(
                check("32项评估命中率", ev.get("hit_rate", 0) >= TH_HIT_RATE,
                      f"{ev.get('hit_rate', 0)*100:.1f}%", f"阈值≥{TH_HIT_RATE*100:.0f}%"))
            report["function"].append(
                check("Top1文件名直击率", True, f"{ev.get('top1_rate', 0)*100:.1f}%"))
            report["performance"].append(
                check("评估平均延迟", ev.get("avg_latency_ms", 99999) < TH_LATENCY_MS,
                      f"{ev.get('avg_latency_ms', 0):.0f}ms"))
            report["_eval"] = ev
        except Exception as e:  # noqa: BLE001
            report["function"].append(check("/eval端点", False, str(e)))
        # 查询改写生效验证
        try:
            r = http_post("/search", {"query": "人工智能应用的审批流程", "top_k": 3}, timeout=60).json()
            report["function"].append(
                check("查询改写生效", len(r.get("rewritten_queries", [])) > 1,
                      f"{len(r.get('rewritten_queries', []))}条查询"))
            report["function"].append(
                check("重排序档位返回", "rerank_mode" in r, r.get("rerank_mode", "?")))
            r2 = http_post("/search", {"query": "外包供应商考核", "top_k": 3,
                                       "expand_context": True}, timeout=60).json()
            ctx_ok = any("[关联片段" in (c.get("context") or "") for c in r2.get("results", []))
            report["function"].append(check("跨文件组装", ctx_ok,
                                            "命中" if ctx_ok else "未触发（可接受）"))
            r3 = http_post("/search", {"query": "供应商台账", "top_k": 3,
                                       "time_filter": "3m"}, timeout=60).json()
            report["function"].append(check("时间过滤生效", "time_filter" in r3,
                                            r3.get("time_filter", "?")))
        except Exception as e:  # noqa: BLE001
            report["function"].append(check("功能抽检", False, str(e)))

        # 新增/修改文件的检索验证（最多10个）；V1.7.13：build_date 必须显式存在才判今日入库
        # （旧版 rec.get("build_date", today) 给无字段文件默认今天，会把 1900 个存量文件
        #  全部当成"今日新建"拉去检索抽检，短数字名天然不命中产生大片误报）
        today = datetime.now().strftime("%Y-%m-%d")
        recent = [rel for rel, rec in state_files.items()
                  if isinstance(rec, dict) and rec.get("build_date") == today]
        for rel in recent[:MAX_NEW_FILE_CHECKS]:
            fname = os.path.splitext(os.path.basename(rel))[0]
            try:
                rr = http_post("/search", {"query": fname, "top_k": 3,
                                          "time_filter": "all"}, timeout=60).json()
                hit = any(fname[:6] in (c.get("source") or "") for c in rr.get("results", []))
                report["function"].append(check(f"新文件检索:{os.path.basename(rel)}", hit,
                                                "命中" if hit else "未命中"))
            except Exception as e:  # noqa: BLE001
                report["function"].append(check(f"新文件检索:{os.path.basename(rel)}", False, str(e)))

    # ---------- 劣化检测（对比上次验证快照） ----------
    baseline = {}
    if os.path.exists(VERIFY_SNAPSHOT_FILE):
        try:
            with open(VERIFY_SNAPSHOT_FILE, encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, json.JSONDecodeError):
            baseline = {}
    if baseline and alive:
        base_latency, base_mem = baseline.get("avg_latency_ms"), baseline.get("memory_mb")
        base_hit, base_top1 = baseline.get("hit_rate"), baseline.get("top1_rate")
        cur = report.get("_eval", {}) or {}
        cur_stats = {}
        try:
            cur_stats = http_get("/stats", timeout=5).json()
        except Exception:  # noqa: BLE001
            pass
        if base_latency and cur_stats.get("avg_latency_ms"):
            bad = cur_stats["avg_latency_ms"] > base_latency * 1.5
            report["performance"].append(
                check("劣化检测:平均延迟", not bad,
                      f"{cur_stats['avg_latency_ms']:.0f}ms（基线{base_latency:.0f}）",
                      "劣化>基线×1.5" if bad else ""))
        if base_mem and cur_stats.get("memory_mb"):
            bad = cur_stats["memory_mb"] > base_mem * 1.3
            report["performance"].append(
                check("劣化检测:内存占用", not bad,
                      f"{cur_stats['memory_mb']}MB（基线{base_mem}）",
                      "劣化>基线×1.3" if bad else ""))
        if base_hit and cur.get("hit_rate") is not None:
            bad = cur["hit_rate"] < base_hit * 0.95
            report["performance"].append(
                check("劣化检测:命中率", not bad,
                      f"{cur['hit_rate']*100:.1f}%（基线{base_hit*100:.1f}%）",
                      "劣化<基线×0.95" if bad else ""))
        if base_top1 and cur.get("top1_rate") is not None:
            bad = cur["top1_rate"] < base_top1 * 0.80
            report["function"].append(
                check("劣化检测:Top1直击率", not bad,
                      f"{cur['top1_rate']*100:.1f}%（基线{base_top1*100:.1f}%）",
                      "劣化<基线×0.80" if bad else ""))

    # ---------- 保存验证快照 ----------
    snapshot = {
        "time": report["time"], "mode": mode,
        "avg_latency_ms": (http_get("/stats", timeout=5).json().get("avg_latency_ms")
                           if alive else None),
        "memory_mb": (http_get("/stats", timeout=5).json().get("memory_mb") if alive else None),
        "hit_rate": (report.get("_eval") or {}).get("hit_rate"),
        "top1_rate": (report.get("_eval") or {}).get("top1_rate"),
    }
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(VERIFY_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return report


def gen_pdf(report, out_path):
    """PDF报告：问题项红色标注，含四维检查表与汇总统计"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    styles = {
        "title": ParagraphStyle("t", fontName="STSong-Light", fontSize=16, spaceAfter=10),
        "h": ParagraphStyle("h", fontName="STSong-Light", fontSize=12, spaceBefore=10, spaceAfter=4),
    }
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            topMargin=40, leftMargin=40, rightMargin=40)
    elems = [Paragraph("RAG 知识库系统每日验证报告", styles["title"]),
             Paragraph(f"模式：{report['mode']}    时间：{report['time']}", styles["h"])]
    total = fails = 0
    for dim, title in [("availability", "可用性"), ("performance", "性能"),
                       ("function", "功能"), ("integrity", "完整性")]:
        items = report.get(dim, [])
        if not items:
            continue
        elems.append(Paragraph(f"■ {title}", styles["h"]))
        data = [["检查项", "结果", "实测值", "说明"]]
        for c in items:
            total += 1
            fails += 0 if c["ok"] else 1
            data.append([c["name"], "通过" if c["ok"] else "异常",
                         str(c["value"]), c.get("detail", "")])
        tbl = Table(data, colWidths=[150, 50, 120, 150])
        style_cmds = [
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A5276")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ]
        for i, c in enumerate(items, 1):   # 问题项红色标注
            if not c["ok"]:
                style_cmds.append(("TEXTCOLOR", (0, i), (-1, i), colors.red))
        tbl.setStyle(TableStyle(style_cmds))
        elems.append(tbl)
    elems.append(Spacer(1, 10))
    elems.append(Paragraph(
        f"汇总：检查 {total} 项，通过 {total - fails} 项，异常 {fails} 项"
        f"（{'健康' if fails == 0 else '需关注'}）", styles["h"]))
    doc.build(elems)
    return total, fails


def main():
    parser = argparse.ArgumentParser(description="RAG 每日验证体系")
    parser.add_argument("--mode", choices=["morning", "evening", "standard"], default="standard")
    parser.add_argument("--no-pdf", action="store_true", help="不生成PDF报告")
    parser.add_argument("--json", action="store_true", help="输出JSON结构化结果（供App调用）")
    args = parser.parse_args()

    report = run_verify(args.mode)
    total = fails = 0
    for dim in ("availability", "performance", "function", "integrity"):
        for c in report.get(dim, []):
            total += 1
            fails += 0 if c["ok"] else 1
    report["summary"] = {"total": total, "passed": total - fails, "failed": fails}

    if args.json:
        pdf_path = None
        if not args.no_pdf:
            out = os.path.join(KB_ROOT, f"verify_report_{datetime.now():%Y%m%d_%H%M}.pdf")
            try:
                gen_pdf(report, out)
                pdf_path = out
            except Exception as e:  # noqa: BLE001
                pdf_path = f"PDF生成失败: {e}"
        report["pdf_report"] = pdf_path
        print(json.dumps(report, ensure_ascii=False))
    else:
        for dim in ("availability", "performance", "function", "integrity"):
            for c in report.get(dim, []):
                mark = "✓" if c["ok"] else "✗"
                print(f"[{mark}] {dim}|{c['name']}: {c['value']} {c.get('detail','')}")
        print(f"\n汇总：检查 {total} 项，异常 {fails} 项")
        if not args.no_pdf:
            out = os.path.join(KB_ROOT, f"verify_report_{datetime.now():%Y%m%d_%H%M}.pdf")
            try:
                gen_pdf(report, out)
                print(f"PDF 报告已生成: {out}")
            except Exception as e:  # noqa: BLE001
                print(f"[!] PDF 生成失败: {e}")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
