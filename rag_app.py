# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 桌面 App「超脑工作搭子」（pywebview 窗口 + FastAPI 后端）
架构：
  - App 进程内嵌 SearchEngine（模型常驻内存，检索毫秒级响应）
  - FastAPI 提供与 rag_server.py 一致的 /health /search /eval /stats 端点
    （外部 watchdog / rag_verify / rag_search 客户端可无缝对接本 App）
  - 索引构建在进程内完成（复用引擎已加载的模型与 collection，无多进程 sqlite 锁）
  - pywebview 拉起原生窗口渲染 app/index.html（全部离线，零外部 CDN 依赖）
用法：python app_main.py  （或双击 超脑工作搭子.app；v4.8 起经轻量入口启动，
     使 spawn 提取子进程不重载本模块的 torch/FastAPI 等重型依赖）
"""
import json
import gc
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from typing import Optional  # noqa: E402

import rag_core  # noqa: E402
from rag_core import (  # noqa: E402
    DOCS_DIR, KB_ROOT, SERVER_HOST, SERVER_PORT, TIME_FILTER_DEFAULT, scan_files,
)
from rag_engine import SearchEngine, EVAL_CASES  # noqa: E402
import rag_writer  # noqa: E402  超脑写手（v5.0）

APP_DIR = os.path.join(KB_ROOT, "app")

# ---------- 全局状态 ----------
engine = None                  # SearchEngine 实例（后台线程加载）
boot = {"phase": "starting", "message": "正在启动 RAG 引擎...", "error": None,
        "t0": time.time(), "switch_result": None}
build_lock = threading.Lock()
build_state = {"running": False}   # v4.8.1：构建运行标志。进度端点只读此标志探测，
                                    # 绝不触碰锁本身（旧实现误 release 了构建线程持有的锁，
                                    # 曾导致两个构建并发争写数据库）

app = FastAPI(title="超脑工作搭子 SuperWork", version="V1.8.5")


# ---------- 请求模型 ----------
class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)   # V1.7.8：上限 20→100，支持全量结果模式（前端分页）
    category: Optional[str] = None
    rerank: bool = True
    rewrite: bool = True
    expand_context: bool = True
    time_filter: str = TIME_FILTER_DEFAULT
    confirm_old: bool = False


class BuildRequest(BaseModel):
    mode: str  # incremental / full / bm25 / text_cache


class TextSearchRequest(BaseModel):
    keywords: Optional[list] = None
    regex: Optional[str] = None
    use_and: bool = False
    grep_mode: bool = False
    context_lines: int = 2
    dir_filter: Optional[str] = None
    limit: int = 20


class VerifyRequest(BaseModel):
    mode: str = "standard"   # morning / evening / standard
    with_pdf: bool = True


class BackupRequest(BaseModel):
    action: str  # backup / full / cleanup


class ModelConfigRequest(BaseModel):
    embed_model: str
    reranker_model: str


@app.get("/api/build_progress")
def api_build_progress():
    """构建进度（build_index 逐文件落盘，前端轮询展示，防'看起来假死'）"""
    try:
        with open(os.path.join(rag_core.DB_DIR, ".rag_build_progress.json"),
                  encoding="utf-8") as f:
            p = json.load(f)
        total, done = p.get("total", 0), p.get("done", 0)
        p["percent"] = round(done / total * 100, 1) if total else 0
        p["building_now"] = bool(engine is not None and build_state["running"])
        return p
    except (OSError, json.JSONDecodeError):
        # V1.7.12：进度文件读取失败时绝不能误报「构建已结束」——构建是否在跑以
        # 内存标志 build_state 为准（不受文件读写竞争影响）。旧版直接返回
        # building_now=False，前端轮询撞上写入瞬间即永久停止，进度卡死在最后一帧。
        running = bool(engine is not None and build_state["running"])
        return {"phase": "idle", "percent": 0, "total": 0, "done": 0,
                "current": "", "chunks_written": 0, "building_now": running}


def require_engine():
    if engine is None:
        raise RuntimeError("引擎尚未就绪，请稍候")
    return engine


# ---------- 引擎加载（后台线程） ----------
def load_engine():
    global engine
    try:
        boot["phase"], boot["message"] = "loading_model", "加载嵌入与重排序模型（首次约 20-60 秒）..."
        t0 = time.time()
        eng = SearchEngine(load_models=True)
        engine = eng
        boot["phase"], boot["message"] = "ready", f"引擎就绪（加载耗时 {time.time()-t0:.0f} 秒）"
        # v5.2.7：强制中断（关窗/杀进程）时 finally 不会执行，进度文件会残留 building/bm25；
        # 启动时归零，避免用户一打开就看见上一次中断留下的假进度
        _save_progress_idle()
        print(f"[App] 引擎就绪，chunks={engine.health().get('chunks')}", flush=True)
    except Exception as e:  # noqa: BLE001
        boot["phase"], boot["error"] = "error", f"引擎加载失败: {e}"
        print(f"[App] 引擎加载失败: {e}", flush=True)


# ================== 兼容 rag_server 的四个端点（8.2） ==================
@app.get("/health")
def api_health():
    if engine is None:
        return {"status": "starting", "chunks": 0, "bm25_loaded": False, "bm25_docs": 0,
                "context_sources": 0, "models_loaded": False,
                "memory_mb": 0.0, "uptime_sec": 0}
    return engine.health()


@app.get("/stats")
def api_stats():
    if engine is None:
        return {"query_count": 0, "avg_latency_ms": 0, "uptime_sec": 0, "memory_mb": 0, "chunks": 0}
    return engine.stats()


@app.post("/search")
def api_search(req: SearchRequest):
    resp = require_engine().search(
        query=req.query, top_k=req.top_k, time_filter=req.time_filter,
        rerank=req.rerank, rewrite=req.rewrite, expand_context=req.expand_context,
        confirm_old=req.confirm_old, category=req.category)
    results = resp.get("results") if isinstance(resp, dict) else None   # V1.7.8：每条补绝对路径（点击打开/访达定位）
    if results:
        for c in results:
            src = c.get("source") or ""
            if src:
                c["path"] = os.path.join(DOCS_DIR, src)
    return resp


@app.post("/eval")
def api_eval():
    return require_engine().run_eval()


# ================== App 专属端点 ==================
@app.middleware("http")
async def _no_store_cache(req, call_next):
    """V1.7.14：统一禁用 HTTP 缓存——本地服务全量返回毫秒级，而缺省省缓存头会让
    WebView/浏览器在"启发式新鲜期"内直接复用缓存的旧页面/旧 API 响应（不发条件
    验证请求），App 升级后界面可能长时间不生效（实测 Playwright 强刷仍命中旧 DOM）。"""
    resp = await call_next(req)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return FileResponse(os.path.join(APP_DIR, "index.html"))


@app.get("/api/boot")
def api_boot():
    return {"phase": boot["phase"], "message": boot["message"], "error": boot["error"],
            "port": CURRENT_PORT, "elapsed": round(time.time() - boot["t0"], 1)}


@app.get("/api/health")
def api_app_health():
    h = api_health()
    # 文档数从状态文件读取（轻量，避免每次轮询全目录扫描）
    try:
        with open(os.path.join(rag_core.KB_ROOT, "docs", ".rag_db_embed", ".rag_state_embed.json"),
                  encoding="utf-8") as f:
            docs_count = len(json.load(f).get("files", {}))
    except (OSError, json.JSONDecodeError):
        docs_count = None
    h.update({"docs_dir": DOCS_DIR, "kb_root": KB_ROOT, "docs_count": docs_count})
    return h


@app.get("/api/dirs")
def api_dirs():
    """一级目录列表（检索过滤/目录树）"""
    dirs = set()
    if os.path.isdir(DOCS_DIR):
        for name in os.listdir(DOCS_DIR):
            if name.startswith("."):
                continue
            if os.path.isdir(os.path.join(DOCS_DIR, name)):
                dirs.add(name)
    if engine is not None:
        for src in engine.ctx_by_source:
            d = src.split("/")[0] if "/" in src else ""
            if d:
                dirs.add(d)
    return {"dirs": sorted(dirs)}


@app.get("/api/files")
def api_files():
    """知识库文档列表（11种受支持格式）"""
    out = []
    if engine is not None:
        chunk_map = {}
        for src, chunks in engine.ctx_by_source.items():
            chunk_map[src] = len(chunks)
    else:
        chunk_map = {}
    for rel in scan_files(DOCS_DIR):
        full = os.path.join(DOCS_DIR, rel)
        try:
            stat = os.stat(full)
            out.append({
                "file": rel, "type": os.path.splitext(rel)[1].lower().lstrip("."),
                "size_kb": round(stat.st_size / 1024, 1),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                "chunks": chunk_map.get(rel, 0),
            })
        except OSError:
            continue
    return {"files": out, "total": len(out)}


@app.post("/api/build")
def api_build(req: BuildRequest):
    """进程内索引构建（复用引擎模型，构建后热重载索引）"""
    return _api_build_impl(req)


def _save_progress_idle():
    """v5.2.4：构建结束（成功或失败）后将进度重置为 idle"""
    try:
        from rag_build_embedding import _save_progress
        _save_progress({"phase": "idle", "total": 0, "done": 0,
                        "current": "", "chunks_written": 0, "t0": 0})
    except Exception:  # noqa: BLE001
        pass


def _sync_text_cache(log=None, progress=None):
    """v5.2.7：增量构建有真实变更时同步刷新全文缓存。

    build_cache() 基于文件指纹增量处理（未变更文件直接复用），实测 1,499 文件约 38 秒。
    修复：旧版全文缓存只在手动重建时更新，新自动收集的文件在「全文检索」中长期搜不到。
    """
    try:
        from build_text_cache import build_cache
        if progress:
            progress({"phase": "text_cache", "total": 0, "done": 0,
                      "current": "同步更新全文缓存（指纹增量，约半分钟）",
                      "chunks_written": 0, "t0": time.time()})
        info = build_cache()
        if log:
            log(f"[i] 全文缓存已增量更新：更新 {info.get('updated', 0)} 个、"
                f"复用 {info.get('kept', 0)} 个，共 {info.get('total_files', 0)} 个文件")
        return info
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"[!] 全文缓存增量更新失败（不影响向量与BM25索引）：{e}")
        return {"error": str(e)}


def _api_build_impl(req: BuildRequest):
    eng = require_engine()
    if not build_lock.acquire(blocking=False):
        return {"error": "已有构建任务进行中，请稍候"}
    build_state["running"] = True   # 立即标记运行中（进度端点据此显示，不依赖锁状态）
    autocollect_state["unindexed"] = 0   # v5.2：本次构建会扫描此刻已存在的收集文件；
                                         # 构建期间新收集的文件重新计数，由下轮自动触发入库
    t_total = time.time()   # v5.2.2：总耗时覆盖向量+BM25+重载全周期（旧版只报向量阶段，
                            # BM25 重建 3 分钟却显示“耗时 0 分钟”造成故障错觉）
    try:
        lines = []
        def log(msg):
            lines.append(str(msg))
            print(msg, flush=True)   # v4.8：同步写 app.log，监测进程据日志增量判定构建活性
        if req.mode == "incremental":
            from rag_build_embedding import build_index, _save_progress
            _, stats = build_index(eng.collection, eng.embed_model, force=False, log=log,
                                   client=eng.chroma_client)
            # 向量库有更新时才重建BM25（双路一致性，12.1夜间流程）。
            # v5.2.5：无新增/无删除时跳过——旧版每次增量都全量重跑 BM25（10.6万 docs
            # 约5-6分钟纯CPU分词），无变更构建被拖成"卡死"观感（实测用户因此关窗中断）
            bm25_needed = stats.get("new_chunks", 0) > 0 or stats.get("removed", 0) > 0
            if bm25_needed:
                _save_progress({"phase": "bm25", "total": stats.get("collection_count", 0),
                                "done": 0, "current": "全部分词并重建BM25索引", "chunks_written": 0,
                                "t0": time.time()})
                from rag_build_bm25 import build_bm25
                bm = build_bm25(log=log)
                if bm:
                    stats.update(bm25_rebuilt=bm["total_docs"])
                # v5.2.7：有真实变更时同步刷新全文缓存，避免新文件在「全文检索」中缺失
                stats["text_cache"] = _sync_text_cache(log, _save_progress)
            else:
                log("[i] 本次无新增/变更 chunks，跳过 BM25 重建（双路索引保持一致，秒级完成）")
        elif req.mode == "full":
            from rag_build_embedding import build_index, _save_progress
            new_col, stats = build_index(eng.collection, eng.embed_model, force=True, log=log,
                                         client=eng.chroma_client)
            eng.collection = new_col   # 全量重建后替换collection引用
            _save_progress({"phase": "bm25", "total": stats.get("collection_count", 0),
                            "done": 0, "current": "全部分词并重建BM25索引", "chunks_written": 0,
                            "t0": time.time()})
            from rag_build_bm25 import build_bm25
            bm = build_bm25(log=log)
            if bm:
                stats.update(bm25_rebuilt=bm["total_docs"])
            # v5.2.7：全量构建后同步刷新全文缓存
            stats["text_cache"] = _sync_text_cache(log, _save_progress)
        elif req.mode == "bm25":
            from rag_build_bm25 import build_bm25
            stats = build_bm25(log=log)
            stats = {"collection_count": eng.collection.count(), **stats}
        elif req.mode == "text_cache":
            from build_text_cache import build_cache
            from rag_build_embedding import _save_progress
            # v5.2.3：缓存重建写入自身进度标记，避免 UI 回退显示上一次 BM25 的陈旧数字
            _save_progress({"phase": "text_cache", "total": 0, "done": 0,
                            "current": "正在提取全部文档文本更新全文缓存（单线程提取，无百分比进度，约10-60分钟）",
                            "chunks_written": 0, "t0": time.time()})
            stats = build_cache()
        else:
            return {"error": f"未知构建模式: {req.mode}"}
        reload_info = eng.reload_index()
        stats.update({"logs": lines, "reloaded": reload_info})
        stats["elapsed_sec"] = round(time.time() - t_total, 1)   # v5.2.2：真实总耗时
        # V1.7.11：完成摘要留存——旧版重置为 idle，前端轮询只能无声停止，长构建
        # （断连/服务重启场景）用户无从得知构建是否结束。done 态 + building_now=false
        # 供前端轮询展示完成提示；下次构建启动时 _save_progress 自然覆盖。
        try:
            from rag_build_embedding import _save_progress
            _save_progress({"phase": "done", "total": 0, "done": 0, "current": "",
                            "chunks_written": 0, "elapsed_sec": stats.get("elapsed_sec", 0),
                            "t0": 0})
        except Exception:  # noqa: BLE001
            _save_progress_idle()
        return stats
    except Exception as e:  # noqa: BLE001
        _save_progress_idle()
        return {"error": f"构建失败: {e}"}
    finally:
        build_state["running"] = False
        build_lock.release()

@app.post("/api/text_search")
def api_text_search(req: TextSearchRequest):
    from build_text_cache import search_text
    resp = search_text(keywords=req.keywords, regex=req.regex, use_and=req.use_and,
                       grep_mode=req.grep_mode, context_lines=req.context_lines,
                       dir_filter=req.dir_filter, limit=req.limit)
    files = resp.get("files") if isinstance(resp, dict) else None   # V1.7.7：结果条目补绝对路径（点击打开/访达定位）
    if files:
        for f in files:
            if f.get("file"):
                f["path"] = os.path.join(DOCS_DIR, f["file"])
    return resp


@app.post("/api/verify")
def api_verify(req: VerifyRequest):
    """每日验证：子进程运行 rag_verify.py --json（访问本 App 的 HTTP 端点做四维检查）"""
    env = dict(os.environ)
    env["RAG_SERVER_PORT"] = str(CURRENT_PORT)
    env["RAG_APP_MODE"] = "1"   # V1.7.13：标记 App 运行形态（守护进程检查项不适用）
    try:
        cmd = [sys.executable, os.path.join(KB_ROOT, "rag_verify.py"),
               "--mode", req.mode, "--json"]
        if not req.with_pdf:
            cmd.append("--no-pdf")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env, cwd=KB_ROOT)
        raw = r.stdout.strip()
        # --json 模式输出整份JSON（忽略之前可能的日志行）
        start = raw.find("{")
        if start < 0:
            return {"error": f"验证失败: {r.stderr[-500:] or r.stdout[-500:]}"}
        report = json.loads(raw[start:])
        report["exit_code"] = r.returncode
        return report
    except subprocess.TimeoutExpired:
        return {"error": "验证超时（300秒），请检查服务状态"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"验证失败: {e}"}


@app.get("/api/backups")
def api_backups():
    """快照列表"""
    import rag_backup
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rag_backup.list_snapshots()
    return {"output": buf.getvalue(),
            "backups_root": rag_backup.BACKUP_ROOT,
            "retention_days": rag_backup.RETENTION_DAYS}


@app.post("/api/backup")
def api_backup(req: BackupRequest):
    import rag_backup
    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if req.action == "backup":
                rag_backup.backup(full=False)
            elif req.action == "full":
                rag_backup.backup(full=True)
            elif req.action == "cleanup":
                rag_backup.cleanup()
            else:
                return {"error": f"未知操作: {req.action}"}
        return {"ok": True, "output": buf.getvalue()}
    except Exception as e:  # noqa: BLE001
        return {"error": f"备份操作失败: {e}"}


@app.get("/api/eval_cases")
def api_eval_cases():
    return {"total": len(EVAL_CASES),
            "cases": [{"query": q, "expected": e} for q, e in EVAL_CASES]}


# ================== 模型配置（App 配置界面） ==================
def _model_info(path_or_name: str) -> dict:
    """读取模型目录信息：名称、维度、大小、类型（embedding/reranker/unknown）"""
    path = path_or_name
    info = {"path": path, "dim": None, "size_mb": None, "kind": "unknown", "loaded": False}
    if not os.path.isdir(path):
        name = path_or_name.split("/")[-1]
        cand = os.path.join(rag_core.LOCAL_MODEL_DIR, name)
        if os.path.isdir(cand):
            path = cand
        else:
            info["name"] = name
            return info
    info["name"] = os.path.basename(path)
    info["path"] = os.path.realpath(path)
    # 维度：config.json hidden_size
    cfg_file = os.path.join(path, "config.json")
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            mcfg = json.load(f)
        info["dim"] = mcfg.get("hidden_size")
        arch = " ".join(mcfg.get("architectures") or [])
        if "SequenceClassification" in arch:
            info["kind"] = "reranker"
        elif os.path.exists(os.path.join(path, "modules.json")):
            info["kind"] = "embedding"
    except (OSError, json.JSONDecodeError):
        pass
    # 大小
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    info["size_mb"] = round(total / 1024 / 1024, 1)
    return info


@app.get("/api/models")
def api_get_models():
    """当前生效模型配置 + 本地 models/ 目录可用模型列表"""
    resolved = {"embed_model": rag_core.EMBED_MODEL, "reranker_model": rag_core.RERANK_MODEL}
    cfg = rag_core.load_model_config()
    local_models = []
    if os.path.isdir(rag_core.LOCAL_MODEL_DIR):
        for name in sorted(os.listdir(rag_core.LOCAL_MODEL_DIR)):
            full = os.path.join(rag_core.LOCAL_MODEL_DIR, name)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "config.json")):
                info = _model_info(full)
                info["loaded"] = (os.path.realpath(full) == os.path.realpath(rag_core.EMBED_MODEL)
                                 or os.path.realpath(full) == os.path.realpath(rag_core.RERANK_MODEL))
                local_models.append(info)
    return {
        "config_file": rag_core.MODEL_CONFIG_FILE,
        "embed_model": _model_info(rag_core.EMBED_MODEL),
        "reranker_model": _model_info(rag_core.RERANK_MODEL),
        "embed_configured": cfg.get("embed_model", "（默认）" + rag_core.DEFAULT_EMBED_MODEL),
        "reranker_configured": cfg.get("reranker_model", "（默认）" + rag_core.DEFAULT_RERANK_MODEL),
        "local_models": local_models,
        "switch_result": boot.get("switch_result"),
    }


def _switch_engine_with_config(old_cfg: dict, embed_changed: bool = False):
    """后台线程：按新配置重建引擎；失败自动回滚配置并保留旧引擎"""
    global engine
    old_engine = engine
    try:
        eng = SearchEngine(load_models=True)
        engine = eng                      # 切换成功：替换引擎
        del old_engine
        gc.collect()
        boot["switch_result"] = {
            "ok": True,
            "embed_changed": embed_changed,
            "message": (f"模型已切换：嵌入 {os.path.basename(rag_core.EMBED_MODEL)} / "
                        f"重排序 {os.path.basename(rag_core.RERANK_MODEL)}"),
        }
        boot["phase"], boot["message"] = "ready", boot["switch_result"]["message"]
        print(f"[App] 模型切换成功: {rag_core.EMBED_MODEL} / {rag_core.RERANK_MODEL}", flush=True)
    except Exception as e:  # noqa: BLE001
        # 加载失败：回滚配置文件并恢复旧模型配置，旧引擎保持可用
        try:
            rag_core.save_model_config(old_cfg)
            rag_core.reload_model_config()
        except Exception:  # noqa: BLE001
            pass
        boot["switch_result"] = {
            "ok": False,
            "embed_changed": False,
            "message": f"新模型加载失败，已回滚为原配置（引擎未中断）: {e}",
        }
        boot["phase"], boot["message"] = "ready", boot["switch_result"]["message"]
        print(f"[App] 模型切换失败已回滚: {e}", flush=True)


@app.post("/api/models")
def api_save_models(req: ModelConfigRequest):
    """保存模型配置并热切换引擎（前端轮询 /api/boot 与 switch_result 获取结果）"""
    old_cfg = rag_core.load_model_config()
    old_embed, old_rerank = rag_core.EMBED_MODEL, rag_core.RERANK_MODEL
    new_cfg = {"embed_model": req.embed_model.strip(), "reranker_model": req.reranker_model.strip()}
    if not new_cfg["embed_model"] or not new_cfg["reranker_model"]:
        return {"error": "嵌入模型与重排序模型均不能为空"}
    rag_core.save_model_config(new_cfg)
    resolved = rag_core.reload_model_config()
    embed_changed = (rag_core.EMBED_MODEL != old_embed)
    # 后台线程切换引擎，期间 boot 显示 switching
    boot["phase"], boot["message"] = "switching", "正在加载新模型并切换引擎（约 10-60 秒）..."
    boot["switch_result"] = None
    boot["t0"] = time.time()
    threading.Thread(target=_switch_engine_with_config, args=(old_cfg, embed_changed), daemon=True).start()
    return {
        "ok": True, "resolved": resolved, "embed_changed": embed_changed,
        "note": ("嵌入模型已变更，检索前需全量重建索引" if embed_changed
                 else "重排序模型变更无需重建索引"),
    }


# ================== 超脑写手（v5.0，OpenAI 兼容大模型 + 知识库素材） ==================
class LLMConfigRequest(BaseModel):
    # 兼容旧字段（单模型时代的公共配置）；V1.5.8 起以 models 清单为准
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    models: Optional[list] = None      # V1.5.8：[{id, alias, model, base_url, api_key, temperature, max_tokens}]
    default_model_id: str = ""         # 默认模型（写作工作台首次默认选中）


class LLMTestRequest(BaseModel):
    model_id: Optional[str] = None     # V1.5.8：按模型测试其独立接口与 Key
    tier: Optional[str] = None         # 兼容 V1.5.7 旧调用，忽略


class WriteRequest(BaseModel):
    doc_type: str = "work_summary"
    topic: str
    points: str = ""
    use_kb: bool = True
    kb_query: str = ""
    top_k: int = 8
    length: str = "medium"     # short / medium / long
    style: str = "formal"      # formal / concise / vivid
    template_file: str = ""        # v5.2.13：仿写模版（知识库内文档相对路径）
    template_local_path: str = ""  # v5.2.14：仿写模版（本机绝对路径，优先于 template_file）
    # V1.5.6：技能 / 专家 / 工作流 / 全自动 / 模型档位 / 附件 / 输出位置
    skill: str = "doc"             # doc / html / pptx / xlsx / research
    expert: str = "general"        # general / gov / tech / bid / data / product
    workflow: str = "standard"     # standard / outline / template
    auto_flow: bool = False        # 全自动执行：先提纲再成稿
    model_id: str = ""             # V1.5.8：所选模型 id（空则用默认模型）
    tier: str = ""                 # 兼容 V1.5.7 旧调用（已不参与选模型）
    attachments: Optional[list] = None   # [{name, text}] 本机附件素材（前端选择时已提取文本）
    output_dir: str = "writer_docs"      # writer_docs / workspace
    # V1.8.4 修订模式：对上一稿提修改意见后重新生成（previous_text 非空即按修订处理）
    previous_text: str = ""        # 上一稿全文（前端在上一轮 done 后收集）
    feedback: str = ""            # 用户的修改意见 / 补充的新要求


class TemplateSearchRequest(BaseModel):
    query: str = ""
    top_k: int = 10


class WriteSaveRequest(BaseModel):
    title: str
    doc_type: str = "custom"
    content: str
    sources: Optional[list] = None


class WriteIdRequest(BaseModel):
    id: str


class ExportDocxRequest(BaseModel):
    title: str = ""
    content: str


class RevealExportRequest(BaseModel):
    filename: Optional[str] = None   # 在访达中选中指定文件（仅接受纯文件名，防路径穿越）
    latest: bool = False             # 在访达中选中最近导出的那个文件
    path: Optional[str] = None       # V1.5.6：选中技能产出文件（校验须位于 writer_docs 或工作空间根内）
    open_file: bool = False          # V1.7.5：用系统默认应用直接打开该文件（标题点击场景）


@app.get("/api/llm_config")
def api_llm_config_get():
    """大模型配置（脱敏返回）+ 模型清单 + 文档类型清单（写作工作台下拉框）"""
    cfg = rag_writer.load_llm_config()
    models = rag_writer.list_models(cfg)
    return {
        "configured": rag_writer.llm_configured(cfg),
        "base_url": cfg["base_url"], "model": cfg["model"],
        "api_key_masked": rag_writer.mask_key(cfg["api_key"]),
        "has_key": bool(cfg["api_key"]),
        "temperature": cfg["temperature"], "max_tokens": cfg["max_tokens"],
        "models": models,
        "default_model_id": cfg.get("default_model_id", ""),
        "doc_types": [{"id": k, "name": v["name"]} for k, v in rag_writer.DOC_TEMPLATES.items()],
    }


@app.post("/api/llm_config")
def api_llm_config_set(req: LLMConfigRequest):
    """保存大模型配置。V1.5.8：models 传则整体覆盖模型清单；
    api_key 留空 = 沿用原 Key（按 id 对应，新条目则继承公共 Key）"""
    cfg = rag_writer.load_llm_config()
    old_models = {m["id"]: m for m in (cfg.get("models") or [])}
    public_key = req.api_key.strip() or cfg.get("api_key", "")
    public_base = req.base_url.strip() or cfg.get("base_url", "")

    if req.models is not None:
        new_models, used = [], set()
        for i, item in enumerate(req.models or []):
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "").strip()
            if not model and not str(item.get("base_url") or "").strip():
                continue                      # 空行跳过
            mid = str(item.get("id") or "").strip() or f"m{i + 1}"
            while mid in used:
                mid = f"{mid}_{i + 1}"
            used.add(mid)
            prev = old_models.get(mid, {})
            key_val = str(item.get("api_key") or "").strip()
            entry = {
                "id": mid,
                "alias": str(item.get("alias") or "").strip(),
                "model": model,
                "base_url": str(item.get("base_url") or "").strip(),
                "api_key": key_val or prev.get("api_key", ""),
            }
            for f, cast in (("temperature", float), ("max_tokens", int)):
                v = item.get(f)
                if v in (None, ""):
                    entry[f] = prev.get(f, cfg.get(f, 0.7 if f == "temperature" else 4096))
                else:
                    try:
                        entry[f] = cast(v)
                    except (TypeError, ValueError):
                        entry[f] = prev.get(f, 0.7 if f == "temperature" else 4096)
            new_models.append(entry)
        cfg["models"] = new_models
        ids = [m["id"] for m in new_models]
        want = req.default_model_id.strip()
        cfg["default_model_id"] = want if want in ids else (ids[0] if ids else "")
        # 顶层字段 = 默认模型（保证旧代码与旧界面仍可用）
        default = next((m for m in new_models if m["id"] == cfg["default_model_id"]), None)
        if default:
            cfg["base_url"] = default["base_url"] or public_base
            cfg["model"] = default["model"]
            cfg["api_key"] = default["api_key"] or public_key
            cfg["temperature"] = default["temperature"]
            cfg["max_tokens"] = default["max_tokens"]
    else:
        # 旧式单模型保存（兼容）
        cfg.update(base_url=public_base, model=req.model.strip(),
                   temperature=req.temperature, max_tokens=req.max_tokens)
        if req.api_key.strip():
            cfg["api_key"] = req.api_key.strip()
        if cfg.get("models"):
            first = cfg["models"][0]
            first.update(model=req.model.strip() or first.get("model", ""),
                         base_url=req.base_url.strip(), temperature=req.temperature,
                         max_tokens=req.max_tokens)
            if req.api_key.strip():
                first["api_key"] = req.api_key.strip()
    rag_writer.save_llm_config(cfg)
    return {"ok": True, "configured": rag_writer.llm_configured(cfg),
            "has_key": bool(cfg["api_key"]), "models": rag_writer.list_models(cfg),
            "default_model_id": cfg.get("default_model_id", "")}


@app.post("/api/llm_test")
def api_llm_test(req: LLMTestRequest = None):
    """测试大模型接口连通性（可指定模型，测其独立接口与 Key）"""
    return rag_writer.test_llm(model_id=(req.model_id if req and req.model_id else ""))


def _load_template_text(rel: str):
    """v5.2.13：从全文缓存读取仿写模版文本。

    只能取缓存中存在的 key（缓存 key 即 docs 下相对路径），天然防路径穿越；
    138MB 缓存按需 json.load 约数秒，写作任务频率低且生成本身耗时更久，可接受。
    """
    if not rel:
        return None
    try:
        from build_text_cache import TEXT_CACHE_FILE
        with open(TEXT_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        entry = cache.get(rel)
        if entry and (entry.get("text") or "").strip():
            return entry["text"]
    except Exception:  # noqa: BLE001
        pass
    return None


@app.post("/api/write_template_search")
def api_write_template_search(req: TemplateSearchRequest):
    """v5.2.13：仿写模版搜索——在知识库中按主题找候选模版文档。

    语义检索找内容相关文档；time_filter=all + confirm_old 放开时间窗
    （模版往往是往年范文，必须能搜到老文档）；同文件多 chunk 去重。"""
    eng = require_engine()
    q = (req.query or "").strip()
    if not q:
        return {"error": "请输入模版搜索关键词"}
    try:
        result = eng.search(q, top_k=max(3, min(req.top_k, 12)), time_filter="all",
                            rerank=True, expand_context=False, confirm_old=True)
        files, seen = [], set()
        for c in (result.get("results") or []):
            src = c.get("source") or ""
            if not src or src in seen:
                continue
            seen.add(src)
            files.append({"file": src, "name": c.get("filename") or src,
                          "score": round(float(c.get("rerank_score") or c.get("score") or 0), 3)})
        return {"ok": True, "files": files[:req.top_k], "query": q}
    except Exception as e:  # noqa: BLE001
        return {"error": f"模版搜索失败: {e}"}


TEMPLATE_LOCAL_EXTS = {".docx", ".doc", ".pdf", ".txt", ".md", ".markdown",
                        ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".html", ".htm"}   # v5.2.14 / V1.7.17：本机模版支持的格式（补入旧版 .ppt）
TEMPLATE_LOCAL_MAX_MB = 20   # 本机模版大小上限（防大文件提取卡死）


@app.post("/api/write_template_pick_local")
def api_write_template_pick_local(payload: Optional[dict] = None):
    """v5.2.14：本机模版选择——弹出 macOS 原生文件选择框，用户选任意位置的文档作仿写模版。

    V1.7.16：按当前技能输出格式筛选样例——选择框只显示兼容类型（文档族/HTML/PPTX/Excel），
    选错格式直接拒绝，保证样例与成品同格式、仿写有意义。
    choose file 为 StandardAdditions 系统命令（不控制其他 App）；取消时 osascript 返回非零；
    选择交由用户完成，不限制目录（桌面/下载/移动硬盘均可），大小在写作时校验。
    """
    out_kind = str(((payload or {}).get("out_kind") or "md")).strip()
    exts = [e for e in rag_writer.sample_exts_for(out_kind) if f".{e}" in TEMPLATE_LOCAL_EXTS]
    if not exts:                      # 未知输出类型：按文档族处理（防御）
        out_kind = "md"
        exts = [e for e in rag_writer.sample_exts_for(out_kind) if f".{e}" in TEMPLATE_LOCAL_EXTS]
    label = rag_writer.SAMPLE_FORMAT_LABELS.get(out_kind, out_kind)
    script = ('POSIX path of (choose file with prompt "选择一篇 %s 格式的仿写样例" '
              'of type {"%s"} without invisibles)' % (label, '","'.join(exts)))
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True, "error": "选择超时（90 秒未完成选择）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"无法弹出选择框: {e}"}
    if r.returncode != 0:
        return {"ok": False, "cancelled": True}   # 用户点“取消”
    path = r.stdout.decode("utf-8", errors="ignore").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "未获取到有效文件"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEMPLATE_LOCAL_EXTS:
        return {"ok": False, "error": f"不支持的格式（{ext}），请选 docx/doc/pdf/xlsx/pptx/ppt/txt/md/csv/html"}
    if ext.lstrip(".") not in exts:
        return {"ok": False,
                "error": f"格式不符：当前技能输出 {label}，样例需为 {'/'.join(exts)} 格式"}
    return {"ok": True, "path": path, "name": os.path.basename(path),
            "size": os.path.getsize(path), "ext": ext.lstrip(".")}


@app.get("/api/write_options")
def api_write_options():
    """V1.5.6：写作工作台选项（技能 Skill / 专家 / 工作流 / 模型 / 输出空间）
    V1.5.8：直接返回已配置模型清单（显示名优先别名）
    V1.8.1：技能只列 内置（含覆盖层）+ 我的技能——TeleAgent 导入层不进入写作台，
    需先在「技能管理」固化；固化后即可选用"""
    from rag_writer import WORKFLOWS, WRITE_EXPERTS

    def _items(d):
        items = []
        for k, v in d.items():
            desc = v.get("desc") or ""
            if v.get("kind") == "imported" and not desc.endswith("）"):
                desc = f"{desc}（来自 TeleAgent 导入）"
            items.append({"id": k, "name": v.get("name"), "icon": v.get("icon"), "desc": desc,
                          "kind": v.get("kind", "builtin"), "out": v.get("out", "md")})
        return items

    models = [{"id": m["id"], "name": m["display"], "alias": m["alias"], "model": m["model"],
               "base_url": m["base_url"], "configured": m["configured"],
               "desc": f'模型：{m["model"]}' + (f'｜接口：{m["base_url"]}' if m["base_url"] else "")}
              for m in rag_writer.list_models()]
    space_root = os.path.dirname(os.path.abspath(rag_core.KB_ROOT))
    return {
        "skills": _items(rag_writer.writable_skills()),   # V1.8.1：写作台只列内置+我的技能，导入层需固化后使用
        "experts": _items(WRITE_EXPERTS),
        "workflows": _items(WORKFLOWS),
        "models": models,
        "spaces": [
            {"id": "writer_docs", "name": "写作成果（writer_docs）"},
            {"id": "workspace", "name": os.path.basename(space_root) or "工作空间"},
        ],
    }


class SkillDeleteRequest(BaseModel):
    id: str


class SkillSaveRequest(BaseModel):
    base_id: str = ""      # 空=新建；内置 key=覆盖层；自定义/导入 id=更新/固化
    name: str
    icon: str = "🧩"
    desc: str = ""
    out: str = "md"        # md / html / pptx / xlsx
    instr: str = ""


@app.get("/api/write_skills")
def api_write_skills():
    """V1.8.0：三层技能库状态（内置 / 我的技能 / 导入），供技能管理页展示"""
    def _short(sid: str, s: dict) -> dict:
        return {"id": sid, "name": s.get("name", ""), "icon": s.get("icon", "🧩"),
                "desc": s.get("desc", ""), "out": s.get("out", "md"),
                "chars": s.get("chars", len(s.get("instr", ""))),
                "updated_at": s.get("updated_at", s.get("imported_at", "")),
                "overridden": bool(s.get("overridden")), "origin": s.get("origin")}
    builtin = rag_writer.builtin_skills()
    custom = rag_writer.custom_skills()
    imported = rag_writer.load_imported_skills()
    return {
        "dirs": rag_writer.teleagent_skill_dirs(),
        "available": bool(rag_writer.teleagent_skill_dirs()),
        "builtin": [_short(k, v) for k, v in builtin.items()],
        "custom": [_short(s.get("id"), s) for s in custom],
        "imported": [_short(s.get("id"), s) for s in imported],
        "builtin_count": len(builtin), "custom_count": len(custom), "imported_count": len(imported),
    }


@app.get("/api/write_skills/detail")
def api_write_skills_detail(id: str):
    """V1.8.0：单个技能完整信息（编辑表单预填，含完整提示词）"""
    s = rag_writer.all_skills().get(id)
    if not s:
        return {"ok": False, "error": "未找到该技能"}
    return {"ok": True, "skill": {
        "id": id, "name": s.get("name"), "icon": s.get("icon"), "desc": s.get("desc", ""),
        "out": s.get("out", "md"), "instr": s.get("instr", ""), "kind": s.get("kind"),
        "contract": s.get("contract", "none"), "overridden": bool(s.get("overridden")),
        "origin": s.get("origin"), "updated_at": s.get("updated_at", "")}}


@app.post("/api/write_skills/import")
def api_write_skills_import():
    """V1.5.9 / V1.8.0：从 TeleAgent 技能目录导入/刷新（只读 SKILL.md；只同步导入层）"""
    if not rag_writer.teleagent_skill_dirs():
        return {"ok": False, "error": "未找到 TeleAgent 技能目录（~/.config/TeleAgent/.../skills）"}
    return rag_writer.refresh_imported_skills()


@app.post("/api/write_skills/save")
def api_write_skills_save(req: SkillSaveRequest):
    """V1.8.0：保存技能（新建我的技能 / 更新 / 固化导入 / 写入内置覆盖层）"""
    return rag_writer.save_skill(req.base_id or "", {"name": req.name, "icon": req.icon,
                                                     "desc": req.desc, "out": req.out,
                                                     "instr": req.instr})


@app.post("/api/write_skills/promote")
def api_write_skills_promote(req: SkillDeleteRequest):
    """V1.8.0：导入技能一键固化为「我的技能」"""
    return rag_writer.promote_imported(req.id)


@app.post("/api/write_skills/reset")
def api_write_skills_reset(req: SkillDeleteRequest):
    """V1.8.0：内置技能恢复出厂（移除覆盖层）"""
    return rag_writer.reset_builtin(req.id)


@app.post("/api/write_skills/delete")
def api_write_skills_delete(req: SkillDeleteRequest):
    """V1.5.9 / V1.8.0：删除我的技能或导入技能（内置不可删，改用恢复出厂）"""
    return rag_writer.delete_skill(req.id)


@app.post("/api/write_attach_pick")
def api_write_attach_pick():
    """V1.5.6：本机文档选作写作附件（macOS 原生多选框），提取文本后返回前端（不落盘）"""
    script = ('set theFiles to choose file with prompt "选择要作为写作素材的附件（可多选，最多5个）" '
              'of type {"doc","docx","pdf","txt","md","markdown","xlsx","xls","pptx","csv","html","htm"} '
              'with multiple selections allowed without invisibles\n'
              'set outText to ""\n'
              'repeat with f in theFiles\n'
              'set outText to outText & (POSIX path of f) & linefeed\n'
              'end repeat\n'
              'return outText')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True, "error": "选择超时（120 秒未完成）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"无法弹出选择框: {e}"}
    if r.returncode != 0:
        return {"ok": False, "cancelled": True}
    paths = [ln.strip() for ln in r.stdout.decode("utf-8", errors="ignore").splitlines() if ln.strip()]
    if not paths:
        return {"ok": False, "cancelled": True}
    files = []
    for path in paths[:5]:
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
            if size > 100 * 1024 * 1024:
                files.append({"name": name, "path": path, "size": size, "chars": 0,
                              "text": "", "error": "超过 100MB，已跳过"})
                continue
            text = rag_core.extract_file(path) or ""
            text = rag_core._clean_noise_text(text)
            if text.strip() and rag_core._readable_ratio(text) < 0.70:
                text = ""   # 与入库侧同门闸：乱码/扫描件不入提示词
            files.append({"name": name, "path": path, "size": size,
                          "chars": len(text), "text": text[:20000],
                          "error": "" if text.strip() else "未能提取有效文本（扫描件/空文档？）"})
        except Exception as e:  # noqa: BLE001
            files.append({"name": name, "path": path, "size": 0, "chars": 0, "text": "",
                          "error": f"解析失败: {e}"})
    return {"ok": True, "files": files}


@app.post("/api/write")
def api_write(req: WriteRequest):
    """流式写作（SSE）：status → sources → delta* → done / error"""
    params = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    # v5.2.13/5.2.14：仿写模版解析——本机路径优先，其次知识库文档；任一失败降级普通写作
    params["template_text"] = ""
    if params.get("template_local_path"):
        path = os.path.abspath(params["template_local_path"])
        name = os.path.basename(path)
        if os.path.splitext(path)[1].lower() not in TEMPLATE_LOCAL_EXTS:
            params["template_load_failed"] = True
            params["template_fail_reason"] = f"本机模版《{name}》格式不支持"
        elif not os.path.isfile(path):
            params["template_load_failed"] = True
            params["template_fail_reason"] = f"本机模版《{name}》不存在（可能已被移动或删除）"
        elif os.path.getsize(path) > TEMPLATE_LOCAL_MAX_MB * 1048576:
            params["template_load_failed"] = True
            params["template_fail_reason"] = f"本机模版《{name}》超过 {TEMPLATE_LOCAL_MAX_MB}MB"
        else:
            try:
                text = rag_core.extract_file(path) or ""
                text = rag_core._clean_noise_text(text)
                if text.strip() and rag_core._readable_ratio(text) >= 0.70:   # 与入库侧同一门闸
                    params["template_text"] = text
                    params["template_name"] = name
                else:
                    params["template_load_failed"] = True
                    params["template_fail_reason"] = f"本机模版《{name}》无法提取有效文本（扫描件/空文档？）"
            except Exception as e:  # noqa: BLE001
                params["template_load_failed"] = True
                params["template_fail_reason"] = f"本机模版《{name}》解析失败: {e}"
    elif params.get("template_file"):
        tpl = _load_template_text(params["template_file"])
        if tpl:
            params["template_text"] = tpl
            params["template_name"] = params["template_file"].rsplit("/", 1)[-1]
        else:
            params["template_text"] = ""   # 加载失败降级为普通写作
            params["template_load_failed"] = True
            params["template_fail_reason"] = f"模版《{params['template_file'].rsplit('/', 1)[-1]}》读取失败（可能已被移出知识库或无文本）"

    def search_fn(query, top_k):
        if engine is None:
            raise RuntimeError("知识库引擎尚未就绪")
        return engine.search(query, top_k=top_k, time_filter="all",
                             rerank=True, expand_context=True)

    return StreamingResponse(rag_writer.stream_write(params, search_fn),
                            media_type="text/event-stream")


@app.get("/api/write_docs")
def api_write_docs():
    return rag_writer.list_docs()


@app.get("/api/write_doc")
def api_write_doc(id: str):
    return rag_writer.get_doc(id)


@app.post("/api/write_save")
def api_write_save(req: WriteSaveRequest):
    if not (req.title or "").strip() or not (req.content or "").strip():
        return {"error": "标题与内容不能为空"}
    return rag_writer.save_doc(req.title.strip(), req.doc_type, req.content, req.sources or [])


@app.post("/api/write_delete")
def api_write_delete(req: WriteIdRequest):
    return rag_writer.delete_doc(req.id)


@app.post("/api/write_export_docx")
def api_write_export_docx(req: ExportDocxRequest):
    """导出 Word（服务端生成 docx 并以下载响应返回）"""
    r = rag_writer.export_docx(req.title, req.content)
    if r.get("ok"):
        return FileResponse(r["path"], filename=r["filename"],
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return {"error": "导出失败"}


@app.post("/api/write_reveal")
def api_write_reveal(req: RevealExportRequest):
    """v5.2.8：在访达中显示导出的 Word 文件或导出目录。

    背景：前端 blob 式下载在 macOS WKWebView 中被静默丢弃，「下载」文件夹里并没有文件；
    后端 writer_docs/exports/ 才是唯一可靠保存点。此端点给前端一个直达入口。
    """
    from rag_writer import EXPORTS_DIR
    if not os.path.isdir(EXPORTS_DIR):
        os.makedirs(EXPORTS_DIR, exist_ok=True)
    try:
        target = None
        if req.path:       # V1.5.6：技能产出文件（限 writer_docs / 工作空间根内，防任意路径）
            from rag_writer import WRITER_DOCS_DIR
            real = os.path.realpath(req.path)
            roots = (os.path.realpath(WRITER_DOCS_DIR),
                     os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            if not any(real == r or real.startswith(r + os.sep) for r in roots):
                return {"error": "路径不在允许范围内"}
            if not os.path.isfile(real):
                return {"error": "文件不存在（可能已被移动或删除）"}
            target = real
        elif req.filename:   # 指定文件 → open -R 在访达中选中
            base = os.path.basename(req.filename)
            cand = os.path.join(EXPORTS_DIR, base)
            if not os.path.isfile(cand):
                return {"error": "指定的导出文件不存在"}
            target = cand
        elif req.latest:   # 最近导出 → 按修改时间取最新 docx
            docs = [os.path.join(EXPORTS_DIR, n) for n in os.listdir(EXPORTS_DIR)
                    if n.lower().endswith(".docx") and os.path.isfile(os.path.join(EXPORTS_DIR, n))]
            if not docs:
                return {"error": "尚未导出过 Word 文件"}
            target = max(docs, key=os.path.getmtime)
        if target:
            if req.open_file:    # V1.7.5：直接用默认应用打开（md→默认编辑器，pptx→PowerPoint/WPS…）
                subprocess.run(["open", target], check=True, timeout=10)
            else:
                subprocess.run(["open", "-R", target], check=True, timeout=10)
        else:              # 默认 → 打开文档主目录（V1.7.4：由 Word 导出目录改为文档目录）
            docs_dir = os.path.dirname(EXPORTS_DIR.rstrip("/"))
            subprocess.run(["open", docs_dir], check=True, timeout=10)
        # 同时给出可点击的完整路径（前端 toast 展示）
        shown = target or os.path.dirname(EXPORTS_DIR.rstrip("/"))
        return {"ok": True, "revealed": shown,
                "dir": shown,
                "shortcut_tip": "文档保存在 rag-kb/writer_docs/（Word 导出在其 exports/ 子目录）"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"打开访达失败: {e}"}


# ================== 自动收集（v5.2：WPS/邮件等打开过的文档自动入库） ==================
import rag_autocollect  # noqa: E402  轻量模块（仅标准库+rag_core常量）

autocollect_state = {"unindexed": 0}   # 已收集但尚未进入索引的文件数


class AutoCollectConfigRequest(BaseModel):
    enabled: bool = True
    interval_min: int = Field(default=10, ge=1, le=1440)
    max_mb: int = Field(default=20, ge=1, le=200)   # V1.6.3：普通文档上限默认 20MB
    lookback_days: int = Field(default=2, ge=1, le=14)


@app.get("/api/autocollect")
def api_autocollect_get():
    """自动收集：配置 + 状态 + 最近收集清单"""
    return {
        "config": rag_autocollect.load_config(),
        "state": rag_autocollect.load_state(),
        "unindexed": autocollect_state["unindexed"],
        "building": build_state["running"],
        "engine_ready": engine is not None,
        "collect_dir": os.path.join("docs", "自动收集"),
    }


@app.post("/api/autocollect_config")
def api_autocollect_config(req: AutoCollectConfigRequest):
    rag_autocollect.save_config(req.model_dump())
    return {"ok": True, "config": rag_autocollect.load_config()}


@app.post("/api/autocollect_scan")
def api_autocollect_scan():
    """立即扫描一次（同步返回结果；有新收集时自动触发增量构建）"""
    r = rag_autocollect.scan_once()
    if r.get("error"):
        return r
    if r.get("collected"):
        autocollect_state["unindexed"] += len(r["collected"])
        _autocollect_maybe_build()
    return r


def _autocollect_maybe_build() -> bool:
    """有待入库文件且引擎就绪且无构建进行中 → 后台线程触发增量构建"""
    if autocollect_state["unindexed"] <= 0 or engine is None or build_state["running"]:
        return False

    def _run():
        resp = api_build(BuildRequest(mode="incremental"))
        if isinstance(resp, dict) and resp.get("error"):
            # 构建未启动（如刚被手动构建抢先）：保留计数，下轮扫描再试
            print(f"[自动收集] 增量构建未启动: {resp['error']}", flush=True)
        else:
            print(f"[自动收集] 收集文件已通过增量构建入库（reloaded={bool(resp.get('reloaded'))}）", flush=True)

    threading.Thread(target=_run, daemon=True).start()
    return True


def autocollect_daemon():
    """后台定时线程：扫描 → 收集 → 自动触发增量构建"""
    while boot["phase"] not in ("ready", "error"):
        time.sleep(5)
    if boot["phase"] == "error":
        print("[自动收集] 引擎加载失败，守护线程退出", flush=True)
        return
    print("[自动收集] 守护线程已启动", flush=True)
    while True:
        cfg = rag_autocollect.load_config()
        try:
            if cfg.get("enabled"):
                r = rag_autocollect.scan_once()
                n = len(r.get("collected", []))
                if n:
                    autocollect_state["unindexed"] += n
                    print(f"[自动收集] 本次收集 {n} 个文件，待入库 {autocollect_state['unindexed']} 个", flush=True)
                _autocollect_maybe_build()
        except Exception as e:  # noqa: BLE001
            print(f"[自动收集] 扫描异常: {e}", flush=True)
        time.sleep(max(30, int(cfg.get("interval_min", 10)) * 60))


# ================== 主程序：端口选择 + uvicorn 线程 + pywebview 窗口 ==================
CURRENT_PORT = SERVER_PORT


def port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((SERVER_HOST, port)) == 0


def sibling_instance_port():
    """v5.3.5 单实例保护：探测 18800-18803 上是否已有本 App 的另一实例在服务。

    判据：/api/health 返回的 docs_dir/kb_root 与本进程相同（同一知识库=同一 ChromaDB）。
    背景（v5.3.4 故障复盘）：旧实例（内存中仍运行旧版扫描逻辑）与新实例并发写同一个
    ChromaDB 时，会把对方新入库的文件当作「已删除」清理索引；且进程被强杀时会把
    HNSW 二进制写成半截，导致引擎加载时段错误（App「意外退出」）。因此双开必须拒绝；
    设置环境变量 RAGKB_ALLOW_MULTI=1 可显式放行（仅限维护排查用途）。
    """
    import urllib.request
    my_docs = os.path.abspath(DOCS_DIR)
    for port in (SERVER_PORT, 18801, 18802, 18803):
        try:
            with urllib.request.urlopen(
                    f"http://{SERVER_HOST}:{port}/api/health", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001  连接失败/非JSON = 不是本 App，跳过
            continue
        if isinstance(data, dict) and \
                os.path.abspath(str(data.get("docs_dir", ""))) == my_docs and \
                data.get("kb_root") == KB_ROOT:
            return port
    return None


def main():
    global CURRENT_PORT
    # v5.3.5：单实例保护——必须先于本进程绑定端口执行，否则会探测到自己
    if not os.environ.get("RAGKB_ALLOW_MULTI"):
        sib = sibling_instance_port()
        if sib:
            msg = (f"检测到另一个实例已在运行（端口 {sib}），本次启动已阻止："
                   "同时运行两个实例会并发写坏知识库索引。请点按 Dock 图标切回原窗口。")
            print(f"[App] {msg}", flush=True)
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'display notification "已在运行，请点 Dock 图标切回窗口；'
                     '同时运行两个会损坏索引，本次启动已阻止" with title "超脑工作搭子"'],
                    timeout=5, check=False)
            except Exception:  # noqa: BLE001  通知失败不影响退出
                pass
            sys.exit(0)
    # 端口选择：默认18800被占用时依次尝试备用端口（App仍可用，外部工具需指定端口）
    CURRENT_PORT = SERVER_PORT
    if port_in_use(CURRENT_PORT):
        for cand in (18801, 18802, 18803):
            if not port_in_use(cand):
                CURRENT_PORT = cand
                break
        print(f"[App] 端口 {SERVER_PORT} 被占用，改用 {CURRENT_PORT}", flush=True)

    config = uvicorn.Config(app, host=SERVER_HOST, port=CURRENT_PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    # 引擎后台加载（窗口先开，页面轮询 /api/boot）
    threading.Thread(target=load_engine, daemon=True).start()

    # v5.2：自动收集守护线程（等引擎就绪后开始定时扫描）
    threading.Thread(target=autocollect_daemon, daemon=True).start()

    # 主线程运行 pywebview（macOS 要求 GUI 必须在主线程）
    import webview
    url = f"http://{SERVER_HOST}:{CURRENT_PORT}/"
    title_suffix = "" if CURRENT_PORT == SERVER_PORT else f"（端口{CURRENT_PORT}）"
    webview.create_window(
        f"超脑工作搭子{title_suffix}", url=url, width=1280, height=860,
        min_size=(1024, 700), background_color="#0f172a",
    )
    print("[App] 窗口已打开，等待用户操作（关闭窗口即退出）...", flush=True)
    webview.start()
    print("[App] 窗口已关闭，退出 App", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
