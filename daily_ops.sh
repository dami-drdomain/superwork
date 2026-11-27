#!/bin/bash
# RAG 知识库系统 —— 每日自动化流程编排（对应技术说明书 12.1）
# 挂载到 cron / launchd 的推荐用法：
#   22:00 夜间同步构建：  .venv/bin/python ...  或  bash daily_ops.sh nightly
#   08:00 晨间验证报告：  bash daily_ops.sh morning
#   全天守护（可选）：    .venv/bin/python rag_watchdog.py （常驻）
set -e
cd "$(dirname "$0")"
PY=".venv/bin/python"

case "${1:-}" in
  nightly)
    # 夜间流程：云端同步（可选 kdocs-cli）→ 向量增量构建 → BM25 索引重建
    echo "=== 夜间增量构建 $(date '+%F %T') ==="
    "$PY" rag_build_embedding.py
    "$PY" rag_build_bm25.py
    "$PY" build_text_cache.py build
    # 常驻服务持有旧索引，构建完成后重启服务加载新索引（若在运行）
    if curl -s -m 3 "http://127.0.0.1:18800/health" >/dev/null 2>&1; then
      "$PY" rag_watchdog.py --restart --quiet
      echo "服务已重启以加载新索引"
    fi
    echo "=== 夜间构建完成 $(date '+%F %T') ==="
    ;;
  morning)
    # 晨间流程：四维晨检 + 新文件检索验证 + PDF 报告
    echo "=== 晨间验证 $(date '+%F %T') ==="
    "$PY" rag_verify.py --mode morning || true
    echo "=== 晨间验证完成 $(date '+%F %T') ==="
    ;;
  evening)
    # 晚间流程：同步完整性检查（跳过35项评估，加速）
    echo "=== 晚间验证 $(date '+%F %T') ==="
    "$PY" rag_verify.py --mode evening || true
    ;;
  *)
    echo "用法: bash daily_ops.sh nightly|morning|evening"
    echo "  nightly  夜间同步构建（向量增量+BM25重建+全文本缓存）"
    echo "  morning  晨间验证报告（性能+功能+可用性）"
    echo "  evening  晚间验证（同步完整性，跳过35项评估）"
    exit 1
    ;;
esac
