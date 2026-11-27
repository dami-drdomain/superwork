# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— BM25 关键词索引构建脚本
按《RAG知识库系统关键技术说明书 v3.0》第五章实现：
  - 数据来源：从 ChromaDB 读取全部 chunk 文本
  - jieba 分词（含自定义词典）→ BM25Okapi 倒排索引
  - pickle 序列化保存 bm25_index.pkl
用法：python rag_build_bm25.py
"""
import os
import pickle
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rag_core import (  # noqa: E402
    DOCS_DIR, BM25_FILE, CHROMA_COLLECTION, tokenize,
)


def build_bm25(docs_dir: str = DOCS_DIR, out_file: str = BM25_FILE, log=print):
    t0 = time.time()
    import chromadb
    from rank_bm25 import BM25Okapi

    client = chromadb.PersistentClient(path=os.path.join(docs_dir, ".rag_db_embed"))
    collection = client.get_or_create_collection(
        CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    total = collection.count()
    if total == 0:
        log("[!] 向量库为空，请先运行 rag_build_embedding.py")
        return None
    # 分批拉取全部 chunks：语料达 10 万+ 后一次性 get() 会触发 SQLite 变量数上限
    # （too many SQL variables），按批次拉取规避（v5.2.1 修复）
    data = {"ids": [], "documents": [], "metadatas": []}
    BATCH = 2000
    offset = 0
    while True:
        batch = collection.get(include=["documents", "metadatas"],
                               limit=BATCH, offset=offset)
        n = len(batch["ids"])
        if n == 0:
            break
        data["ids"].extend(batch["ids"])
        data["documents"].extend(batch["documents"])
        data["metadatas"].extend(batch["metadatas"])
        offset += n
        if n < BATCH:
            break
    log(f"[i] 分批拉取完成，共 {len(data['ids'])} 个 chunk")

    doc_ids, doc_map, corpus = [], {}, []
    for cid, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
        meta = meta or {}
        source = meta.get("source", "")
        chunk_idx = meta.get("chunk_idx", 0)
        doc_id = f"{source}__{chunk_idx}"
        doc_ids.append(doc_id)
        doc_map[doc_id] = {
            "source": source, "chunk_idx": chunk_idx, "text": doc,
            "chunk_type": meta.get("chunk_type", ""),
            "file_type": meta.get("file_type", ""),
            "top_dir": meta.get("top_dir", ""),
            "filename": meta.get("filename", ""),
            "build_date": meta.get("build_date", ""),
            "file_mtime": meta.get("file_mtime", 0.0),
        }
        corpus.append(tokenize(doc))

    log(f"[i] 对 {len(corpus)} 个 chunk 分词并构建 BM25 索引 ...")
    bm25_index = BM25Okapi(corpus)
    payload = {
        "index": bm25_index,
        "doc_ids": doc_ids,
        "doc_map": doc_map,
        "total_docs": len(doc_ids),
        "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(out_file, "wb") as f:
        pickle.dump(payload, f)
    log(f"[✓] BM25 索引构建完成：{len(doc_ids)} docs，"
        f"耗时 {time.time()-t0:.0f}s，已保存 {out_file}")
    return {"total_docs": len(doc_ids), "elapsed_sec": round(time.time() - t0, 1),
            "bm25_file": out_file}


if __name__ == "__main__":
    build_bm25()
