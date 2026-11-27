# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 全文本缓存检索（混合检索的补充手段）
按《RAG知识库系统关键技术说明书 v3.0》第十一章实现：
  - 全文本缓存：扫描全部文档提取纯文本存 JSON，增量更新（文件大小+修改时间指纹）
  - 单文件最大提取 200,000 字符，统一过滤 base64 图片数据
  - 搜索模式：多关键词（AND/OR） / 正则 / grep模式（按行提取上下文） / 目录过滤
  - 模糊匹配：文本归一化去除引号、括号、书名号等分隔符号
  - search_text() 返回结构化结果（供 App 调用）；CLI 模式打印展示
用法：
  python build_text_cache.py build                        # 构建/更新缓存
  python build_text_cache.py search --keywords 网络安全 应急
  python build_text_cache.py search --keywords 应急 --and
  python build_text_cache.py search --regex "应急.{0,10}预案"
  python build_text_cache.py search --keywords 应急 --grep --context 2
  python build_text_cache.py search --keywords 外包 --dir 制度规范
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rag_core import (  # noqa: E402
    DOCS_DIR, TEXT_CACHE_FILE, scan_files, extract_file, _clean_noise_text,
    _readable_ratio,
)

MAX_CACHE_CHARS = 200_000   # 单文件最大提取200,000字符
_CTX_DIRTY = str.maketrans("", "", "\"'“”‘’《》〈〉【】[]()（）")   # 归一化：去引号/括号/书名号


def normalize(text: str) -> str:
    """文本归一化：去除引号、括号、书名号等分隔符号，使被符号断开的短语可连续匹配"""
    return text.translate(_CTX_DIRTY)


# ---------- 缓存构建（11.1） ----------
def build_cache(docs_dir: str = DOCS_DIR):
    cache = {}
    if os.path.exists(TEXT_CACHE_FILE):
        try:
            with open(TEXT_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}
    updated = kept = 0
    seen = set()   # v5.2.7：记录本次扫描到的文件，用于清理已删除文件的残留条目
    sources = [(rel, os.path.join(docs_dir, rel)) for rel in scan_files(docs_dir)]
    for rel, full in sources:
        seen.add(rel)
        try:
            stat = os.stat(full)
            fp = f"{stat.st_size}_{int(stat.st_mtime)}"
        except OSError:
            continue
        old = cache.get(rel)
        if old and old.get("fingerprint") == fp:
            kept += 1
            continue
        text = extract_file(full) or ""
        text = _clean_noise_text(text)[:MAX_CACHE_CHARS]
        # v5.2.9：乱码门闸（与入库侧 rag_core.build_chunks_for_file 一致）——
        # 解析失败回退读到二进制乱码的文本不进缓存，避免「全文检索」命中后展示乱码片段
        if text and _readable_ratio(text) < 0.70:
            cache.pop(rel, None)
            continue
        cache[rel] = {"fingerprint": fp, "mtime": int(stat.st_mtime), "text": text}
        updated += 1
    # v5.2.7：旧版直接保存旧缓存，已删除文件永远留在缓存里，「全文检索」会返回不存在的文档
    stale = [k for k in cache if k not in seen]
    if stale:
        for k in stale:
            cache.pop(k, None)
    os.makedirs(os.path.dirname(TEXT_CACHE_FILE), exist_ok=True)
    # v5.3.3 修复：原子写（tmp + rename）——非原子写时，构建任务写缓存的窗口期内
    # 并发读者（全文检索等）会读到半截 JSON 抛 Expecting value 导致任务中断
    tmp = TEXT_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, TEXT_CACHE_FILE)
    return {"updated": updated, "kept": kept, "removed": len(stale), "total_files": len(cache),
            "cache_file": TEXT_CACHE_FILE}


# ---------- 缓存搜索（11.2）----------
def search_text(keywords=None, regex=None, use_and=False, grep_mode=False,
                context_lines=2, dir_filter=None, limit=20):
    """缓存搜索，返回结构化结果 {hits_total, files: [{file, snippets|lines}]}"""
    if not os.path.exists(TEXT_CACHE_FILE):
        return {"error": "缓存不存在，请先构建全文本缓存", "hits_total": 0, "files": []}
    with open(TEXT_CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)

    kws = [normalize(k) for k in keywords] if keywords else []
    if not kws and not regex:
        return {"error": "需要提供关键词或正则", "hits_total": 0, "files": []}
    try:
        pattern = re.compile(regex) if regex else None
    except re.error as e:
        return {"error": f"正则表达式错误: {e}", "hits_total": 0, "files": []}

    def line_match(norm_line):
        if pattern:
            return bool(pattern.search(norm_line))
        if use_and:
            return all(k in norm_line for k in kws)
        return any(k in norm_line for k in kws)

    files_out, hits_total = [], 0
    for rel in sorted(cache.keys()):
        if dir_filter and dir_filter not in rel:
            continue
        text = cache[rel]["text"]
        norm_text = normalize(text)
        matched = False
        spans = []
        if pattern:
            if pattern.search(norm_text):
                matched = True
                spans = [m.span() for m in pattern.finditer(norm_text)][:3]
        else:
            if use_and:
                matched = all(k in norm_text for k in kws)
            else:
                matched = any(k in norm_text for k in kws)
            for k in kws:
                pos = norm_text.find(k)
                if pos >= 0 and len(spans) < 3:
                    spans.append((pos, pos + len(k)))
        if not matched:
            continue
        hits_total += 1
        if len(files_out) >= limit:
            continue
        entry = {"file": rel}
        if grep_mode:
            lines = text.split("\n")
            norm_lines = [normalize(l) for l in lines]
            matched_idx = [i for i, nl in enumerate(norm_lines) if line_match(nl)]
            shown, out_lines = set(), []
            for i in matched_idx[:30]:
                for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                    if j not in shown:
                        shown.add(j)
                        out_lines.append({"n": j + 1, "text": lines[j], "hit": j == i})
            entry["lines"] = out_lines
        else:
            entry["snippets"] = [f"...{text[max(0, s-80):min(len(text), e+80)]}..." for s, e in spans]
        files_out.append(entry)
    return {"hits_total": hits_total, "files": files_out}


def _print_result(result):
    if result.get("error"):
        print(f"[!] {result['error']}")
        return
    for entry in result["files"]:
        print(f"\n===== {entry['file']} =====")
        if "lines" in entry:   # grep模式
            for l in entry["lines"]:
                mark = ">" if l["hit"] else " "
                print(f"{mark} {l['n']}: {l['text']}")
        else:
            for s in entry.get("snippets", []):
                print(f"  {s}")
            if not entry.get("snippets"):
                print("  （正文含关键词）")
    print(f"\n共命中 {result['hits_total']} 个文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全文本缓存构建与检索")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="构建/增量更新全文本缓存")
    p_s = sub.add_parser("search", help="缓存搜索")
    p_s.add_argument("--keywords", nargs="+", help="多关键词（默认OR逻辑）")
    p_s.add_argument("--and", action="store_true", help="关键词AND逻辑")
    p_s.add_argument("--regex", help="正则表达式匹配")
    p_s.add_argument("--grep", action="store_true", help="grep模式，按行提取上下文")
    p_s.add_argument("--context", type=int, default=2, help="grep模式上下文行数")
    p_s.add_argument("--dir", help="按相对路径过滤")
    p_s.add_argument("--limit", type=int, default=20, help="最多展示文件数")
    args = parser.parse_args()
    if args.cmd == "build":
        r = build_cache()
        print(f"[✓] 全文本缓存完成：更新 {r['updated']}，未变 {r['kept']}，"
              f"共 {r['total_files']} 文件 → {r['cache_file']}")
    else:
        if not args.keywords and not args.regex:
            parser.error("search 需要 --keywords 或 --regex")
        _print_result(search_text(
            keywords=args.keywords, regex=args.regex, use_and=args.__dict__.get("and"),
            grep_mode=args.grep, context_lines=args.context,
            dir_filter=args.dir, limit=args.limit))
