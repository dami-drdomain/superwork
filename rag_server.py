# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 常驻检索服务
按《RAG知识库系统关键技术说明书 v3.0》第八章实现：
  - FastAPI + Uvicorn，绑定本机回环地址（默认 127.0.0.1:18800）
  - 服务启动流程（8.3）：离线变量 → 嵌入模型 → 重排序模型 → ChromaDB
    → BM25 索引 → 上下文组装缓存 → 启动服务
  - API（8.2）：/health /search /eval /stats
用法：python rag_server.py [--port 18800] [--host 127.0.0.1]
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})  # 8.3 Step1

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")  # warning（生产模式）

from typing import Optional

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from rag_core import SERVER_HOST, SERVER_PORT, TIME_FILTER_DEFAULT  # noqa: E402
from rag_engine import SearchEngine  # noqa: E402

app = FastAPI(title="RAG 知识库检索服务", version="v4.6")
engine = None  # 由 main() 初始化


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    category: Optional[str] = None    # 按一级目录过滤
    rerank: bool = True               # 是否启用三档重排序
    rewrite: bool = True              # 是否启用查询改写
    expand_context: bool = True       # 是否启用上下文组装
    time_filter: str = TIME_FILTER_DEFAULT  # all / 3m / 6m / 1y
    confirm_old: bool = False         # 包含一年以上旧文档


@app.post("/search")
def api_search(req: SearchRequest):
    return engine.search(
        query=req.query, top_k=req.top_k, time_filter=req.time_filter,
        rerank=req.rerank, rewrite=req.rewrite, expand_context=req.expand_context,
        confirm_old=req.confirm_old, category=req.category,
    )


@app.get("/health")
def api_health():
    return engine.health()


@app.get("/stats")
def api_stats():
    return engine.stats()


@app.post("/eval")
def api_eval():
    """运行32项评估用例，返回命中率（10.4）"""
    return engine.run_eval()


def main():
    global engine
    parser = argparse.ArgumentParser(description="RAG 常驻检索服务")
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    args = parser.parse_args()

    t0 = time.time()
    print("[i] 启动 RAG 检索服务：加载模型与索引（首次约 45-60 秒）...", flush=True)
    engine = SearchEngine()
    print(f"[✓] 引擎就绪（{time.time()-t0:.0f}s），启动 HTTP 服务 {args.host}:{args.port}", flush=True)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
