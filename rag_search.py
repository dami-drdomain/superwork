# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 命令行检索客户端（含本地回退机制）
按《RAG知识库系统关键技术说明书 v3.0》8.4 实现：
  - 先探测 /health（2秒超时）：服务可用 → HTTP调用检索（30秒超时，毫秒级响应）
  - 服务不可用 → 自动回退本地模型加载模式（慢，但保证可用）
  - 命令行默认 time_filter='all'（不限时间）
用法：
  python rag_search.py "查询内容"
  python rag_search.py "查询内容" --top-k 3 --json
  python rag_search.py "查询内容" --time-filter 3m
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rag_core import SERVER_HOST, SERVER_PORT  # noqa: E402

HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/health"
SEARCH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/search"


def service_available(timeout=2.0):
    """8.4：调用前先检测 /health 端点（2秒超时）"""
    import requests
    try:
        r = requests.get(HEALTH_URL, timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def search_via_service(query, params, timeout=30.0):
    """服务可用 → HTTP调用检索接口（30秒超时）"""
    import requests
    r = requests.post(SEARCH_URL, json={"query": query, **params}, timeout=timeout)
    r.raise_for_status()
    return r.json(), "service"


def search_local_fallback(query, params):
    """本地回退：服务不可用时自动切换本地模型加载模式（慢，但保证可用）"""
    from rag_engine import SearchEngine
    print("[i] 服务不可用，回退本地模式（加载模型中，约45秒）...", file=sys.stderr)
    engine = SearchEngine(load_models=True)
    return engine.search(query=query, **params), "local"


def main():
    parser = argparse.ArgumentParser(description="RAG 知识库检索客户端")
    parser.add_argument("query", help="查询内容")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--time-filter", default="all", choices=["all", "3m", "6m", "1y"],
                        help="默认 all（不限时间）")
    parser.add_argument("--category", default=None, help="按一级目录过滤")
    parser.add_argument("--no-rerank", action="store_true", help="禁用重排序")
    parser.add_argument("--no-rewrite", action="store_true", help="禁用查询改写")
    parser.add_argument("--no-context", action="store_true", help="禁用上下文组装")
    parser.add_argument("--confirm-old", action="store_true", help="包含一年以上旧文档")
    parser.add_argument("--json", action="store_true", help="输出原始JSON")
    args = parser.parse_args()

    params = {
        "top_k": args.top_k,
        "time_filter": args.time_filter,
        "rerank": not args.no_rerank,
        "rewrite": not args.no_rewrite,
        "expand_context": not args.no_context,
        "confirm_old": args.confirm_old,
        "category": args.category,
    }

    if service_available():
        result, mode = search_via_service(args.query, params)
    else:
        result, mode = search_local_fallback(args.query, params)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"查询: {result.get('query')}")
    print(f"改写: {result.get('rewritten_queries')}  意图: {result.get('intent')}  "
          f"重排序档位: {result.get('rerank_mode')}  耗时: {result.get('elapsed_ms')}ms  模式: {mode}")
    if result.get("time_filter_note"):
        print(f"[时间过滤] {result['time_filter_note']}")
    if result.get("low_relevance"):
        print("[结果] 未找到相关内容（Top-1 相似度低于 0.3）")
        return
    for i, c in enumerate(result.get("results", []), 1):
        print(f"\n--- 结果{i} [{c['chunk_type']}] {c['source']} (块{c['chunk_idx']}) "
              f"vec={c['vec_sim']} rrf={c['rrf_score']} rerank={c['rerank_score']}")
        print(c["text"][:500])
        if c.get("context") and len(c["context"]) > len(c["text"]):
            print(f"  [上下文片段] {c['context'][:200]} ...")


if __name__ == "__main__":
    main()
