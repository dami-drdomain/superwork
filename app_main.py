# -*- coding: utf-8 -*-
"""
RAG 知识库 App —— 轻量启动入口（v4.8）
真正实现在 rag_app.py，本文件仅作 __main__ 入口且保持极轻（只 import os/sys）。
原因：spawn 并行提取子进程（rag_build_embedding.iter_extract）会按 __main__
路径重导入本模块——轻入口使 worker 启动 <1 秒，且不重复加载
torch/FastAPI/ChromaDB（8GB 内存友好）。若直接以 rag_app.py 作为 __main__，
每个提取 worker 都会多加载一份 ~400MB 重型依赖。

用法（与原 rag_app.py 等价）：
  .venv/bin/python app_main.py        # 或双击 RAG知识库.app
"""
import os
import sys

# v4.8.1：App 进程 spawn 提取子进程前已用过 tokenizer —— 关闭其 fork 警告
# （spawn 内部经 fork+exec 实现，tokenizers 的 atfork 钩子会在 app.log 刷屏）
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rag_app
    rag_app.main()
