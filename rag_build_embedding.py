# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 向量索引构建（增量/全量）
按《RAG知识库系统关键技术说明书 v3.0》第四章实现：
  - 全量重建（force）与增量构建（MD5+文件大小双比对，4.4）
  - 变更文件旧chunk删除 + 僵尸索引清理（v4.5修复）
  - 分批编码（批64）与分批写入（批500，4.3）
  - build_index() 可被 App 进程内调用（传入已有 collection/model，避免重复加载模型）
  - v4.8 提取并行化：ProcessPoolExecutor 4 进程并行提取（提取是构建瓶颈：
    串行实测仅约 1 文件/76秒，1055 个待处理文件需并行才能当日完成）
用法：
  python rag_build_embedding.py            # 增量构建
  python rag_build_embedding.py --force    # 全量重建
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})

import rag_core  # 动态引用模型配置（配置界面切换模型后正确记录）
from rag_core import (  # noqa: E402
    DOCS_DIR, DB_DIR, STATE_FILE, CHROMA_COLLECTION,
    EMBED_BATCH_SIZE, CHROMA_WRITE_BATCH,
    scan_files, file_md5, build_chunks_for_file,
)

PROGRESS_FILE = os.path.join(DB_DIR, ".rag_build_progress.json")

# ---------- v4.8 并行提取参数（8GB 内存约束：worker 仅导入 rag_core 轻量依赖，不含 torch/ChromaDB） ----------
EXTRACT_WORKERS = min(4, os.cpu_count() or 1)   # 提取子进程数（spawn 启动，内存占用极小）
EXTRACT_WINDOW = max(EXTRACT_WORKERS * 4, 8)    # 在途预取窗口：大文件慢时其余 worker 不饿死
SPAWN_GUARD_SEC = 420                           # 首个结果守护超时：防 spawn 挂死导致整晚空转


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"model": rag_core.EMBED_MODEL, "build_time": "", "total_chunks": 0, "files": {}}


def save_state(state):
    os.makedirs(DB_DIR, exist_ok=True)
    # v5.3.5 原子写（tmp + os.replace）：进程被强杀不再留下半截 JSON，
    # 否则 load_state 解析失败回退空态，海量文件被误判待处理/待删除
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _save_progress(payload: dict):
    """构建进度落盘（App 前端轮询展示，防'看起来假死'）。
    V1.7.12：临时文件 + 原子替换——旧版直接截断写，前端轮询撞上写入瞬间会读到
    空文件/半截 JSON，api_build_progress 异常分支返回 building_now=False，
    前端轮询误判「构建已结束」而永久停止，进度卡死在最后一帧
    （实测 14:13 的 4/168 帧在页面上卡了 4 小时，构建实际早已正常完成）。"""
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, PROGRESS_FILE)
    except OSError:
        pass


def _full(rel, docs_dir):
    """语料源路径解析：rel 为 docs 相对路径"""
    return os.path.join(docs_dir, rel)


def _extract_one(rel, docs_dir):
    """单文件提取（串行路径与子进程共用的同一封装，永不抛异常）"""
    return rag_core.extract_file_safe(_full(rel, docs_dir))


def iter_extract(to_process, docs_dir: str = DOCS_DIR, log=print):
    """
    提取迭代器（v4.8.1 构建提速核心）：
      - ProcessPoolExecutor(spawn) 多进程并行提取，主进程专注分块/编码/写库（流水线化）
      - spawn 子进程仅导入 rag_core（轻量，不加载 torch/ChromaDB；App 需轻量入口 app_main.py）
      - 完成即消费（wait/FIRST_COMPLETED）：大文件（9MB级xlsx/扫描件pdf提取需数分钟）不阻塞
        其他已完成文件的处理，worker 恒饱和不空转（有序消费曾被单文件拖住整条流水线）
      - 消费顺序即完成顺序（分块ID按文件内容寻址，顺序不影响正确性；进度 done 仍单调递增）
      - 窗口预取（EXTRACT_WINDOW）：主进程编码期间 worker 持续提取不空转
      - 逐项容错（单项失败本地兜底）+ 进程池整体异常自动回退串行，构建永不因提取层中断
    """
    if EXTRACT_WORKERS <= 1 or len(to_process) < 8:   # 少量文件不值得起进程池
        for rel in to_process:
            _, text, mtime = _extract_one(rel, docs_dir)
            yield rel, text, mtime
        return

    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
    import multiprocessing as mp

    ex = None
    try:
        ctx = mp.get_context("spawn")   # 不用 fork：GUI/多线程进程 fork 在 macOS 不安全
        ex = ProcessPoolExecutor(max_workers=EXTRACT_WORKERS, mp_context=ctx)
    except Exception as e:  # noqa: BLE001
        log(f"[!] 提取进程池创建失败（{e}），回退串行提取")

    if ex is None:
        for rel in to_process:
            _, text, mtime = _extract_one(rel, docs_dir)
            yield rel, text, mtime
        return

    log(f"[i] 并行提取启动：{EXTRACT_WORKERS} 进程（spawn 轻量 worker），预取窗口 {EXTRACT_WINDOW}，完成即消费")
    yielded = set()
    try:
        it = iter(to_process)
        in_flight = {}   # future → rel

        def _submit():
            rel = next(it, None)
            if rel is not None:
                fut = ex.submit(rag_core.extract_file_safe, _full(rel, docs_dir))
                in_flight[fut] = rel

        for _ in range(EXTRACT_WINDOW):
            _submit()

        first = True
        while in_flight:
            # 等待任意完成（首个结果限时守护：防 spawn 挂死整晚空转）
            done_set, _ = wait(list(in_flight), timeout=SPAWN_GUARD_SEC if first else None,
                               return_when=FIRST_COMPLETED)
            if not done_set:
                raise RuntimeError(
                    f"提取子进程 {SPAWN_GUARD_SEC}s 无任何结果，判定进程池不可用")
            first = False
            for fut in done_set:
                rel = in_flight.pop(fut)
                try:
                    _, text, mtime = fut.result()
                except Exception as e:  # noqa: BLE001  # 单项失败本地兜底，语义与原串行一致
                    log(f"[!] 提取子进程异常 {rel}: {e}（本地兜底提取）")
                    _, text, mtime = _extract_one(rel, docs_dir)
                yielded.add(rel)
                yield rel, text, mtime
                _submit()   # 消费一个立即补一个，窗口恒满、worker 恒饱和
    except Exception as e:  # noqa: BLE001  # 进程池崩溃等罕见情况：剩余文件回退串行
        log(f"[!] 并行提取中断（{e}），剩余 {len(to_process) - len(yielded)} 个文件回退串行")
        for rel in to_process:
            if rel in yielded:
                continue
            _, text, mtime = _extract_one(rel, docs_dir)
            yield rel, text, mtime
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)   # 正常结束/中断均不等待在途任务
        except Exception:  # noqa: BLE001
            pass


def build_index(collection, model, docs_dir: str = DOCS_DIR, force: bool = False,
                log=print, client=None):
    """
    索引构建核心函数（可被 CLI 与 App 进程内调用）。
    collection/model 由调用方传入：CLI 自建；App 复用引擎已加载实例（避免重复加载模型）。
    client：ChromaDB 客户端，force 全量重建删除集合时使用（App 传引擎的 chroma_client）。
    v4.7：逐文件保存状态（中断后增量构建可续，无需从头重来）+ 进度文件实时落盘。
    v4.8：提取多进程并行（iter_extract），分块/编码/写库仍在主进程（复用 App 已加载模型）。
    返回 (collection, stats)：force 重建后 collection 为新引用，调用方需替换。
    """
    t0 = time.time()
    if force:
        if client is None:
            client = getattr(collection, "client", None) or getattr(collection, "_client", None)
        try:
            client.delete_collection(CHROMA_COLLECTION)
            log("[i] 已删除旧集合，开始全量重建 ...")
        except Exception:  # noqa: BLE001
            pass
        collection = client.get_or_create_collection(
            CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    # ---------- 4.4 增量更新机制：扫描 + MD5/大小比对 ----------
    old_state = load_state()
    old_files = old_state.get("files", {})
    rel_files = scan_files(docs_dir)
    n_docs = len(rel_files)
    log(f"[i] 扫描到 {n_docs} 个受支持文件")

    to_process, unchanged = [], {}
    for rel in rel_files:
        full = _full(rel, docs_dir)
        rec = old_files.get(rel)
        if force or rec is None:   # 全量重建时所有文件都需重新编码
            to_process.append(rel)
            continue
        try:
            if rec.get("size") == os.path.getsize(full) and rec.get("mtime") == os.path.getmtime(full):
                unchanged[rel] = rec   # V1.7.11：大小+修改时间均未变 → 直接跳过
                continue                #（旧版对每个未变更文件仍全量读盘算 MD5，1700+ 文件每轮多耗 10-15 秒）
            # 疑似变更（size 或 mtime 变动、或旧 state 无 mtime 字段）→ 用 MD5 精确复核，
            # 避免 touch/复制保留时间戳造成的误重扫；复核通过时刷新 mtime，下轮即可短路
            if rec.get("size") == os.path.getsize(full) and rec.get("md5") == file_md5(full):
                unchanged[rel] = {**rec, "mtime": os.path.getmtime(full)}  # 哈希+大小均未变 → 跳过
                continue
        except OSError:
            pass
        to_process.append(rel)

    removed = sorted(set(old_files.keys()) - set(rel_files))
    log(f"[i] 待处理 {len(to_process)} 个（新增/变更），未变更 {len(unchanged)} 个，已删除 {len(removed)} 个")

    # ---------- 删除变更文件旧chunk + 僵尸索引清理（v4.5） ----------
    # v4.6性能修复：仅对"已索引过且已变更"的文件执行delete；
    # 新文件（从未入库）无旧chunk可删，跳过delete避免海量无效查询
    changed = [rel for rel in to_process if rel in old_files]
    log(f"[i] 需清理旧chunk的变更文件 {len(changed)} 个，已删除文件 {len(removed)} 个")
    for rel in changed + removed:
        try:
            collection.delete(where={"source": rel})
        except Exception as e:  # noqa: BLE001
            log(f"[!] 清理旧chunk失败 {rel}: {e}")

    # ---------- 4.3 向量写入策略：提取→分块→编码→分批写入 ----------
    build_date = datetime.now().strftime("%Y-%m-%d")
    # V1.7.11：内存压力预警——8GB 机器上 WPS/浏览器等大内存应用导致 swap 换页抖动时，
    # 编码吞吐可暴跌数十倍（实测 2 小时仅完成 4 个文件）。提前告警，用户可关闭大应用后重试。
    mem_warn = ""
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 2**30
        if avail_gb < 1.5:
            mem_warn = (f"系统可用内存仅 {avail_gb:.1f}GB（低于 1.5GB），编码速度可能显著下降；"
                        f"建议先关闭 WPS/浏览器等大内存应用，或稍后重试")
            log(f"[⚠] {mem_warn}")
    except Exception:  # noqa: BLE001
        pass
    new_state_files = dict(unchanged)
    total_chunks = 0
    processed_files = 0
    no_text_files = []   # v5.2.9：无法提取有效文本的文件（乱码门闸/最小文本量拦截）
    processed_list = []
    t_build = time.time()
    _save_progress({"phase": "building", "total": len(to_process), "done": 0,
                     "current": "", "chunks_written": 0, "t0": t_build, "warn": mem_warn})
    for rel, text, mtime in iter_extract(to_process, docs_dir, log=log):
        _save_progress({"phase": "building", "total": len(to_process), "done": processed_files,
                        "current": rel, "chunks_written": total_chunks, "t0": t_build, "warn": mem_warn})
        full = _full(rel, docs_dir)
        chunks = build_chunks_for_file(rel, text, mtime, build_date) if text else []
        if not chunks:
            # v5.2.9：无法提取有效文本的文件（扫描版PDF/老格式乱码/空表格等，被 rag_core
            # 乱码门闸或 MIN_FILE_TEXT 拦截）也写入状态并标记 no_text。旧版在这里 pop 掉、
            # 不记录 → 每轮增量都重新列为"待处理"，12 秒空转后显示"成功完成"，永远循环。
            # 写入后下轮按未变更跳过；文件日后被替换/OCR 后（md5 变化）会自动重新处理。
            new_state_files[rel] = {"md5": file_md5(full), "size": os.path.getsize(full),
                                    "mtime": os.path.getmtime(full), "no_text": True,
                                    "build_date": build_date}   # V1.7.13：补 build_date（供验证模块「新文件检索」检查当日入库文件，旧版从未记录致该项静默失效）
            no_text_files.append(rel)
            processed_files += 1
            # 逐文件落盘，保持 v4.7 断点续建语义：中断后已标记的不再重扫
            save_state({
                "model": rag_core.EMBED_MODEL,
                "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "build_type": "force" if force else "incremental",
                "total_chunks": collection.count(),
                "files": new_state_files,
            })
            continue
        texts = [c["text"] for c in chunks]
        embs = model.encode(texts, normalize_embeddings=True,
                            batch_size=EMBED_BATCH_SIZE, show_progress_bar=False)
        for i in range(0, len(chunks), CHROMA_WRITE_BATCH):
            batch = chunks[i:i + CHROMA_WRITE_BATCH]
            collection.upsert(
                ids=[c["id"] for c in batch],
                embeddings=[e.tolist() for e in embs[i:i + CHROMA_WRITE_BATCH]],
                documents=[c["text"] for c in batch],
                metadatas=[c["metadata"] for c in batch],
            )
        total_chunks += len(chunks)
        processed_files += 1
        processed_list.append({"file": rel, "chunks": len(chunks)})
        log(f"[+] {rel}: {len(chunks)} chunks")
        # v4.7 断点续建：逐文件保存状态，任何时刻中断后增量构建均可从断点继续
        new_state_files[rel] = {"md5": file_md5(full), "size": os.path.getsize(full),
                                "mtime": os.path.getmtime(full), "build_date": build_date}   # V1.7.13：补 build_date（同上）
        save_state({
            "model": rag_core.EMBED_MODEL,
            "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "build_type": "force" if force else "incremental",
            "total_chunks": collection.count(),
            "files": new_state_files,
        })

    if removed:
        log(f"[i] 已清理 {len(removed)} 个已删除文件的僵尸索引")
    if no_text_files:
        log(f"[i] {len(no_text_files)} 个文件无法提取有效文本（扫描版PDF/老格式/空表格等），"
            f"已标记跳过，不再重复处理；文件日后变更时会自动重试")

    new_state = {
        "model": rag_core.EMBED_MODEL,
        "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "build_type": "force" if force else "incremental",
        "total_chunks": collection.count(),
        "files": new_state_files,
    }
    save_state(new_state)
    _save_progress({"phase": "done", "total": len(to_process), "done": processed_files,
                    "current": "", "chunks_written": total_chunks,
                    "elapsed_sec": round(time.time() - t_build, 1)})
    log(f"[✓] 构建完成：处理 {processed_files} 个文件，新增 {total_chunks} chunks，"
        f"集合总量 {collection.count()} chunks，总耗时 {time.time()-t0:.0f}s")
    stats = {
        "processed_files": processed_files,
        "new_chunks": total_chunks,
        "no_text_files": len(no_text_files),
        "collection_count": collection.count(),
        "removed": len(removed),
        "skipped": len(unchanged),
        "elapsed_sec": round(time.time() - t0, 1),
        "details": processed_list,
    }
    return collection, stats  # force重建后collection为新引用，调用方需替换


def main():
    parser = argparse.ArgumentParser(description="RAG 向量索引构建（增量/全量）")
    parser.add_argument("--force", action="store_true", help="全量重建（删除集合并重新创建）")
    parser.add_argument("--docs-dir", default=DOCS_DIR, help="知识库文档目录")
    args = parser.parse_args()

    import chromadb
    from sentence_transformers import SentenceTransformer

    os.makedirs(args.docs_dir, exist_ok=True)
    client = chromadb.PersistentClient(path=os.path.join(args.docs_dir, ".rag_db_embed"))
    collection = client.get_or_create_collection(
        CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    print("[i] 加载嵌入模型 ...")
    model = SentenceTransformer(rag_core.EMBED_MODEL)
    build_index(collection, model, docs_dir=args.docs_dir, force=args.force, client=client)


if __name__ == "__main__":
    main()
