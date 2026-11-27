# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 检索引擎层
按《RAG知识库系统关键技术说明书 v3.0》实现：
  - 查询改写：同义词词典 / 复合查询拆解 / 意图分类（第七章）
  - 同义词向量融合（6.3，v4.6核心技术）
  - 双路召回 + RRF 融合 + 同文件去重 + 递进式时间过滤（6.1 / 6.2）
  - 三档重排序机制（6.4）+ 低质量结果过滤（6.6）
  - 上下文组装（6.5，含跨文件补充）
  - 评估框架（10.4，32项用例）与 /health /stats 数据
"""
import gc
import os
import re
import time
import pickle
import logging
from datetime import datetime

import numpy as np

import rag_core  # 动态引用模型配置（配置界面切换模型后 reload_model_config 生效）

from rag_core import (
    DOCS_DIR, DB_DIR, BM25_FILE, OFFLINE_ENV,
    VECTOR_DIM, CHROMA_COLLECTION,
    RRF_K, SUBQUERY_WEIGHT, FILENAME_MATCH_BOOST, BM25_ONLY_BOOST,
    BM25_ONLY_BOOST_MAX, BM25_ONLY_LIGHTWEIGHT_FLOOR, SKIPPED_THRESHOLD, LIGHTWEIGHT_THRESHOLD,
    FULL_FILTER_THRESHOLD, LIGHTWEIGHT_FILTER_THRESHOLD, NORERANK_VEC_FILTER,
    MIN_RELEVANCE, RERANK_TOP_N, RERANK_FNAME_BONUS,
    LW_W_VEC, LW_W_RRF, LW_W_FNAME,
    LW_W_TIME, RERANK_TIME_BONUS, TIME_DECAY_HALF_LIFE_DAYS,   # V1.8.5：时效软加权
    SYN_VEC_FUSION_WEIGHT, SYN_VEC_FUSION_MAX, MAX_SYNONYM_GROUPS,
    MAX_SUBQUERIES, CANDIDATE_LIMIT,
    CTX_PREV_TAIL, CTX_NEXT_HEAD, CTX_MAX_LEN,
    CROSS_FILE_MAX_FILES, CROSS_FILE_SNIPPET,
    TIME_FILTER_LADDER, TIME_FILTER_DEFAULT,
    tokenize, init_jieba,
)

logger = logging.getLogger("rag_kb")
os.environ.update(OFFLINE_ENV)  # 8.3 服务启动流程 Step1：离线环境变量

# ============================================================
# 同义词词典（7.1）—— 28 组通用领域映射，可按 13.5 定制化扩充
# ============================================================
SYNONYM_DICT = {
    "人工智能": ["AI", "人工智能技术"],
    "机器学习": ["ML", "机器学习技术"],
    "深度学习": ["DL"],
    "大语言模型": ["大模型", "LLM"],
    "检索增强生成": ["RAG", "检索增强生成技术"],
    "知识库": ["知识库系统", "KB"],
    "混合检索": ["双路检索", "混合检索技术"],
    "重排序": ["精排", "重排序模型"],
    "向量数据库": ["向量库", "向量存储"],
    "语义检索": ["向量检索", "语义搜索"],
    "关键词检索": ["BM25检索", "关键词搜索"],
    "查询改写": ["查询重写", "查询扩展"],
    "同义词扩展": ["同义词词典", "同义扩展"],
    "上下文组装": ["上下文拼接", "上下文构建"],
    "增量构建": ["增量索引", "增量更新"],
    "全量重建": ["全量索引", "重建索引"],
    "守护进程": ["看门狗", "watchdog"],
    "应急预案": ["应急响应预案", "应急处置预案"],
    "网络安全": ["信息安全", "网络安全防护"],
    "数据治理": ["数据管理", "数据治理体系"],
    "外包管理": ["外包治理", "外包合规管理"],
    "外包": ["外包服务", "服务外包"],
    "供应商": ["合作方", "服务商"],
    "考核评价": ["绩效评价", "考核评估"],
    "故障处置": ["故障处理", "故障应对"],
    "告警": ["预警", "监控告警"],
    "备份": ["数据备份", "备份策略"],
    "会议纪要": ["会议记录", "纪要"],
}

# 反向索引：词汇 → 同组其他词（双向映射）
_SYN_REVERSE = {}
for _std, _alts in SYNONYM_DICT.items():
    _group = [_std] + list(_alts)
    for _w in _group:
        _SYN_REVERSE.setdefault(_w, [])
        for _o in _group:
            if _o != _w and _o not in _SYN_REVERSE[_w]:
                _SYN_REVERSE[_w].append(_o)
_SYN_ALL_WORDS = sorted(_SYN_REVERSE.keys(), key=len, reverse=True)  # 长词优先匹配

# 文件名匹配扩展映射（7.2）：只添加不会泛化的精确映射
FNAME_EXPANSION_MAP = {
    "AI": ["人工智能"],
    "应急": ["应急预案", "应急处置"],
    "安全": ["安全管理制度"],
    "白皮书": ["产品介绍"],
    "台账": ["清单", "名单"],
    "架构": ["技术架构", "系统架构"],
}

# 意图分类（7.4）
FILENAME_INTENT_KEYWORDS = ["名单", "清单", "台账", "报告", "方案", "制度", "办法", "规范", "指南", "手册", "白皮书", "流程"]
_FILLER_WORDS = set("的了呢吧吗是在和与或啊呀之其把被地得也很就都还")
_STOPWORDS = {"以及", "关于", "如何", "怎么", "什么", "哪些", "请问", "一下", "之后", "然后", "如果", "但是", "以及"}
# V1.7.10：文档体裁词——在 BM25-only boost 判定中不计入命中数。
# 这些词几乎出现在任意公文/作业/通知里，无区分度：实测「我的养老会议发言稿」被寒假作业
# 文档以「会议+发言稿」2/3 凑数误触发 +10 加权并霸榜前两名。仅影响 boost 判定，
# 不影响 BM25 召回/文件名匹配/上下文组装等其他用途。
_GENRE_WORDS = {
    "会议", "纪要", "会议纪要", "发言稿", "讲话稿", "讲话", "演讲稿", "汇报", "报告",
    "总结", "计划", "方案", "通知", "通报", "请示", "批复", "意见", "函", "简报",
    "材料", "清单", "台账", "提纲", "说明", "要求", "介绍", "白皮书", "说明书",
    "手册", "指南", "申报书", "申请书", "协议", "合同", "细则",
}

SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def extract_keywords(query: str):
    """提取查询实词关键词（用于文件名匹配/精确匹配/跨文件组装）"""
    kws = [t for t in tokenize(query) if len(t.strip()) >= 2 and t not in _STOPWORDS]
    # 同名长词优先，去重
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def classify_intent(query: str) -> str:
    """查询意图分类（7.4）：filename_search vs content_search"""
    if any(k in query for k in FILENAME_INTENT_KEYWORDS):
        return "filename_search"
    if len(query.strip()) <= 4 and not any(w in query for w in _FILLER_WORDS):
        return "filename_search"
    return "content_search"


def split_compound(query: str):
    """复合查询拆解（7.3）：对标/关于/和 三类模式"""
    subs = []
    if "对标" in query:
        left, right = query.split("对标", 1)
        if left.strip():
            subs.append(left.strip())
        if right.strip():
            subs.append("对标" + right.strip())
        if left.strip():
            subs.append(left.strip() + " 优化建议")
    elif "关于" in query:
        body = query.split("关于", 1)[1]
        body = re.sub(r"的(通知|说明|要求|规定|方案)$", "", body.strip())
        if body.strip():
            subs.append(body.strip())
    elif "和" in query and len(query) <= 20:
        left, right = query.split("和", 1)
        if left.strip() and right.strip():
            subs.extend([left.strip(), right.strip()])
    return subs


def match_synonyms(query: str):
    """同义词匹配（7.1）：长词优先 + 非重叠匹配，最多 MAX_SYNONYM_GROUPS 组"""
    matched, occupied = [], []
    for word in _SYN_ALL_WORDS:
        if len(matched) >= MAX_SYNONYM_GROUPS:
            break
        start = query.find(word)
        if start < 0:
            continue
        end = start + len(word)
        if any(not (end <= s or start >= e) for s, e in occupied):
            continue  # 非重叠匹配：跳过已匹配区域
        occupied.append((start, end))
        matched.append((start, end, word, _SYN_REVERSE[word]))
    matched.sort()
    return matched


def rewrite_query(query: str):
    """
    Step 0 查询改写（6.1）：
    返回 (queries, syn_for_fusion, intent)
      queries[0] 为主查询（权重1.0），其余为扩展子查询（权重0.6）
      syn_for_fusion 为用于向量融合的同义词（6.3）
    """
    intent = classify_intent(query)
    matched = match_synonyms(query)
    sub_queries = []
    for start, end, word, syns in matched:
        for syn in syns[:1]:  # 每组取一个代表同义词生成子查询
            sub_queries.append(query[:start] + syn + query[end:])
    sub_queries.extend(split_compound(query))
    # 去重、去除与主查询相同者，限制上限（7.3）
    seen, uniq = {query}, []
    for q in sub_queries:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    queries = [query] + uniq[:MAX_SUBQUERIES]
    syn_for_fusion = [syns[0] for _, _, _, syns in matched[:SYN_VEC_FUSION_MAX]]
    return queries, syn_for_fusion, intent


def expand_filename_keywords(keywords):
    """文件名匹配扩展（7.2）：关键词 + 精确扩展映射"""
    out = list(keywords)
    for k in keywords:
        for exp in FNAME_EXPANSION_MAP.get(k, []):
            if exp not in out:
                out.append(exp)
    return out


# ============================================================
# 评估框架（10.4）：32 项评估用例 (query, expected)
# v5.2.6 刷新：剔除 3 个目标内容已不在语料的失效用例
# （网络安全事件如何分级 / 故障处置的响应时限 / 系统故障如何上报）
# 命中定义：expected 出现在结果的文件名 / 目录 / 内容中即命中
# ============================================================
EVAL_CASES = [
    ("人工智能应用的管理原则", "人工智能"),
    ("AI应用的审批流程是怎样的", "审批"),
    ("大模型使用的安全要求", "安全要求"),
    ("人工智能应用的禁止行为", "禁止"),
    ("外包管理的职责分工", "外包"),
    ("外包供应商如何考核评价", "考核"),
    ("外包人员的数据安全责任", "数据安全"),
    ("项目验收标准", "验收"),
    ("数据分类分级标准", "分类分级"),
    ("数据安全管理办法", "数据安全"),
    ("数据治理的组织架构", "数据治理"),
    ("数据质量要求", "数据质量"),
    ("网络安全应急预案的启动条件", "应急预案"),
    ("应急响应的组织架构", "应急"),
    ("故障演练要求", "演练"),
    ("供应商台账", "供应商"),
    ("供应商名单", "供应商"),
    ("合作方的评分标准", "评分"),
    ("知识库技术架构", "技术架构"),
    ("混合检索的实现原理", "混合检索"),
    ("RRF融合算法是什么", "RRF"),
    ("向量数据库选型说明", "向量数据库"),
    ("重排序模型的作用", "重排序"),
    ("文档分块策略", "分块"),
    ("嵌入模型介绍", "嵌入"),
    ("知识库系统建设会议", "会议"),
    ("系统建设的里程碑安排", "里程碑"),
    ("产品白皮书", "白皮书"),
    ("产品的主要功能", "主要功能"),
    ("检索增强生成技术", "RAG"),
    ("部署环境要求", "部署"),
    ("监控告警机制", "告警"),
]


# ============================================================
# SearchEngine：常驻检索引擎（第八章）
# ============================================================
class SearchEngine:
    """检索引擎：模型 + ChromaDB + BM25 + 上下文缓存的统一封装"""

    def __init__(self, docs_dir: str = None, load_models: bool = True):
        self.docs_dir = docs_dir or DOCS_DIR
        self.started_at = time.time()
        self.query_count = 0
        self.latency_sum = 0.0
        self._models_loaded = False
        self._load_bm25()
        self._connect_chroma()
        self._build_context_cache()
        if load_models:
            self.load_models()

    # ---------- 8.3 服务启动流程 ----------
    def load_models(self):
        """Step2/3：加载 Embedding 模型（约45秒）与 Reranker 模型（约10秒）
        模型来源为 rag_core.EMBED_MODEL / RERANK_MODEL（受 App 配置界面控制）"""
        if self._models_loaded:
            return
        from sentence_transformers import SentenceTransformer, CrossEncoder
        logger.info("加载嵌入模型 %s ...", rag_core.EMBED_MODEL)
        self.embed_model = SentenceTransformer(rag_core.EMBED_MODEL)
        logger.info("加载重排序模型 %s ...", rag_core.RERANK_MODEL)
        self.reranker = CrossEncoder(rag_core.RERANK_MODEL, max_length=512)
        self._models_loaded = True

    def _connect_chroma(self):
        """Step4：连接 ChromaDB（PersistentClient）"""
        import chromadb
        client = chromadb.PersistentClient(path=os.path.join(self.docs_dir, ".rag_db_embed"))
        self.collection = client.get_or_create_collection(
            CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        self.chroma_client = client

    def _load_bm25(self):
        """Step5：加载 BM25 索引（pickle 反序列化）"""
        self.bm25_data = None
        if os.path.exists(BM25_FILE):
            try:
                with open(BM25_FILE, "rb") as f:
                    self.bm25_data = pickle.load(f)
                logger.info("BM25 索引已加载: %d docs", self.bm25_data.get("total_docs", 0))
            except Exception as e:  # noqa: BLE001
                logger.warning("BM25 索引加载失败: %s", e)

    def _build_context_cache(self):
        """Step6：构建上下文组装缓存（source→chunks 映射 + 按目录分组）

        v5.3.1 修复：chroma 无 ids 全量 get 受 SQLite 变量数上限限制，chunks≈10万+ 时抛
        InternalError "too many SQL variables"，旧版被 except 静默吞掉后 ctx 缓存恒为空，
        检索的相邻块拼接/跨文件补充（6.4 上下文组装）与 /api/dirs 的来源目录
        一直静默失效——改为先拉全量 ids（include=[] 不触发超限），再按 ids 分批拉取。
        """
        self.ctx_by_source = {}
        data = {"ids": [], "documents": [], "metadatas": []}
        try:
            total = self.collection.count()
        except Exception:  # noqa: BLE001
            total = 0
        if total:
            try:
                all_ids = self.collection.get(include=[])["ids"]
            except Exception as e:  # noqa: BLE001
                logger.warning("上下文缓存拉取 ids 失败: %s", e)
                all_ids = []
            batch = 20000   # 单批 ids 数（SQLite 变量上限 32766，留余量）
            for i in range(0, len(all_ids), batch):
                try:
                    part = self.collection.get(
                        ids=all_ids[i:i + batch], include=["documents", "metadatas"])
                except Exception as e:  # noqa: BLE001
                    logger.warning("上下文缓存第 %d 批拉取失败（跳过该批）: %s", i // batch, e)
                    continue
                data["ids"].extend(part.get("ids", []))
                data["documents"].extend(part.get("documents", []))
                data["metadatas"].extend(part.get("metadatas", []))
        for cid, doc, meta in zip(data.get("ids", []), data.get("documents", []), data.get("metadatas", [])):
            src = (meta or {}).get("source", "")
            if not src:
                continue
            self.ctx_by_source.setdefault(src, []).append({
                "chunk_id": cid, "text": doc or "", **(meta or {}),
            })
        for chunks in self.ctx_by_source.values():
            chunks.sort(key=lambda c: c.get("chunk_idx", 0))
        # 按一级目录分组（供跨文件组装）
        self.ctx_by_dir = {}
        for src, chunks in self.ctx_by_source.items():
            top_dir = chunks[0].get("top_dir", "") if chunks else ""
            self.ctx_by_dir.setdefault(top_dir, {})[src] = chunks
        logger.info("上下文缓存构建完成: %d sources", len(self.ctx_by_source))

    def reload_index(self):
        """索引构建后热重载：重载 BM25 索引与上下文组装缓存（App 内联构建后调用）"""
        self._load_bm25()
        self._build_context_cache()
        return {
            "bm25_docs": self.bm25_data.get("total_docs", 0) if self.bm25_data else 0,
            "context_sources": len(self.ctx_by_source),
            "chunks": self.collection.count(),
        }

    # ---------- 模型推理 ----------
    def encode(self, texts, batch_size=64):
        """bge 编码（归一化向量，cosine 内积等价）"""
        return self.embed_model.encode(texts, normalize_embeddings=True,
                                       batch_size=batch_size, show_progress_bar=False)

    # ---------- 检索管线（6.1 流程总览） ----------
    def search(self, query: str, top_k: int = 5, time_filter: str = TIME_FILTER_DEFAULT,
               rerank: bool = True, rewrite: bool = True, expand_context: bool = True,
               confirm_old: bool = False, category: str = None) -> dict:
        t_start = time.time()
        query = (query or "").strip()
        if not query:
            return {"query": query, "results": [], "error": "空查询"}

        # Step 0: 查询改写（7.x）
        if rewrite:
            queries, syn_for_fusion, intent = rewrite_query(query)
        else:
            queries, syn_for_fusion, intent = [query], [], classify_intent(query)
        keywords = extract_keywords(query)
        fname_kws = expand_filename_keywords(keywords)
        boost_mult = 3.0 if intent == "filename_search" else 1.0  # 意图影响文件名权重

        # Step 0.5: 同义词向量融合（6.3）——主查询向量 = 0.65原语义 + 0.35同义词语义
        main_emb = np.asarray(self.encode([query])[0], dtype=np.float32)
        for syn_word in syn_for_fusion[:SYN_VEC_FUSION_MAX]:
            syn_emb = np.asarray(self.encode([syn_word])[0], dtype=np.float32)
            main_emb = (1 - SYN_VEC_FUSION_WEIGHT) * main_emb + SYN_VEC_FUSION_WEIGHT * syn_emb
        norm = float(np.linalg.norm(main_emb))
        if norm > 0:
            main_emb = main_emb / norm  # 重新归一化

        # ---------- 双路召回 + RRF 融合（6.2） ----------
        pool = {}   # doc_id -> candidate（统一键：source__chunk_idx，与 5.2 对齐）
        vec_hit_ids = set()
        pool_vec_ids = set()
        bm25_only_boosted = set()  # BM25-only boost 每候选只享受一次，不跨子查询累加
        pool_max_vec = 0.0         # 向量路最高相似度：强命中时不再启用BM25-only加权（交叉印证）

        def _fname_boost(meta, mult):
            """文件名/目录名匹配 boost"""
            fn, td = meta.get("filename", ""), meta.get("top_dir", "")
            for k in fname_kws:
                if k and (k in fn or (td and k in td)):
                    return FILENAME_MATCH_BOOST * mult
            return 0.0

        def _add(doc_id, meta, text, score, vec_sim):
            if doc_id in pool:
                pool[doc_id]["rrf_score"] += score
                if vec_sim > pool[doc_id]["vec_sim"]:
                    pool[doc_id]["vec_sim"] = vec_sim
            else:
                pool[doc_id] = {
                    "doc_id": doc_id, "rrf_score": score, "vec_sim": vec_sim,
                    "text": text or "", "meta": meta or {},
                }

        try:
            chroma_total = self.collection.count()
        except Exception:  # noqa: BLE001
            chroma_total = 0
        n_results = min(max(18, top_k * 4), chroma_total) if chroma_total else 0

        for qi, q in enumerate(queries):
            weight = 1.0 if qi == 0 else SUBQUERY_WEIGHT
            # Step 1: 向量语义检索
            if n_results > 0:
                emb = main_emb if qi == 0 else np.asarray(self.encode([q])[0], dtype=np.float32)
                try:
                    kwargs = {
                        "query_embeddings": [emb.tolist()], "n_results": n_results,
                        "include": ["documents", "metadatas", "distances"],
                    }
                    if category:
                        kwargs["where"] = {"top_dir": category}
                    res = self.collection.query(**kwargs)
                except Exception:  # noqa: BLE001
                    res = None
                if res:
                    ids = res.get("ids", [[]])[0]
                    dists = res.get("distances", [[]])[0]
                    docs = res.get("documents", [[]])[0]
                    metas = res.get("metadatas", [[]])[0]
                    for rank, (cid, dist, doc, meta) in enumerate(zip(ids, dists, docs, metas), 1):
                        meta = meta or {}
                        # 统一候选键：向量路与BM25路对齐为 source__chunk_idx（5.2）
                        key = f"{meta.get('source', '')}__{meta.get('chunk_idx', 0)}"
                        vec_sim = max(0.0, 1.0 - float(dist))
                        pool_max_vec = max(pool_max_vec, vec_sim)
                        score = weight * (1.0 / (RRF_K + rank)) + _fname_boost(meta, boost_mult)
                        vec_hit_ids.add(key)
                        pool_vec_ids.add(key)
                        _add(key, meta, doc, score, vec_sim)
            # Step 2: BM25 关键词检索
            if self.bm25_data and self.bm25_data.get("doc_ids"):
                bm25_index = self.bm25_data["index"]
                doc_ids = self.bm25_data["doc_ids"]
                tokens = tokenize(q)
                if tokens:
                    try:
                        scores = np.asarray(bm25_index.get_scores(tokens))
                    except Exception:  # noqa: BLE001
                        scores = np.zeros(len(doc_ids))
                    order = np.argsort(-scores)[: top_k * 2]
                    for rank, ti in enumerate(order, 1):
                        if scores[ti] <= 0:
                            break
                        did = doc_ids[ti]
                        info = self.bm25_data["doc_map"].get(did)
                        if not info:
                            continue
                        if category and info.get("top_dir") != category:
                            continue
                        text = info.get("text", "")
                        score = weight * (1.0 / (RRF_K + rank))
                        vec_sim = pool[did]["vec_sim"] if did in pool else 0.0
                        # BM25-only boost：仅BM25发现且高比例关键词精确命中（6.2）；
                        # 向量路已有强命中(≥skipped阈值)时不再加权，遵循交叉印证。
                        # v5.2.3 修复（BM25 满血后评估回归 100%→85.7%）：
                        #   每查询最多加权 BM25_ONLY_BOOST_MAX 个候选——旧版不设上限时
                        #   加权候选以 10.0 碾压向量路全部真实命中（RRF≈0.016），
                        #   10万+语料下泛词组合大量误触发。关键词门槛保持60%不变。
                        # V1.7.10 修复：kw_hits 只统计非体裁词（_GENRE_WORDS）——旧口径下
                        #   「会议/发言稿」等体裁泛词凑数即可触发（实测「我的养老会议发言稿」
                        #   被寒假作业文档 2/3 命中误 boost +10 并霸榜前两名），收紧后必须
                        #   命中查询主体词（如「养老」「大模型」）才能触发 boost。
                        effective_kws = [k for k in keywords if k and k not in _GENRE_WORDS]
                        kw_hits = sum(1 for k in effective_kws if k in text)
                        need = min(len(effective_kws), max(2, (len(effective_kws) * 6 + 9) // 10))
                        exact = kw_hits >= need > 0
                        if (exact and did not in vec_hit_ids
                                and did not in bm25_only_boosted
                                and len(bm25_only_boosted) < BM25_ONLY_BOOST_MAX
                                and pool_max_vec < SKIPPED_THRESHOLD):
                            bm25_only_boosted.add(did)
                            score += BM25_ONLY_BOOST
                            vec_sim = max(vec_sim, BM25_ONLY_LIGHTWEIGHT_FLOOR)  # 低向量补偿
                            _boosted_this_line = True
                        else:
                            _boosted_this_line = False
                        if did not in pool:
                            meta = {
                                "source": info.get("source", ""),
                                "chunk_idx": info.get("chunk_idx", 0),
                                "chunk_type": info.get("chunk_type", "paragraph"),
                                "file_type": info.get("file_type", ""),
                                "top_dir": info.get("top_dir", ""),
                                "filename": info.get("filename", ""),
                                "build_date": info.get("build_date", ""),
                                "file_mtime": info.get("file_mtime", 0.0),
                            }
                        else:
                            meta = pool[did]["meta"]
                        _add(did, meta, text, score, vec_sim)
                        if _boosted_this_line:
                            pool[did]["_bm25_boosted"] = True   # V1.7.10：打标，轻量档排序时回退真实 RRF
            # 子查询间不互相视为"向量命中"，恢复为向量路真实命中集
            vec_hit_ids = set(pool_vec_ids)

        if not pool:
            return self._finalize(query, [], {"queries": queries, "intent": intent},
                                  t_start, time_filter, rerank_mode="none", note=None)

        # ---------- Step 3: 合并排序 + Step 4: 同文件去重 ----------
        cand_limit = max(CANDIDATE_LIMIT, min(top_k * 2, 200))   # V1.7.8：大 top_k（全量结果模式）时按需扩候选池；默认 top_k 下限≥30 保持既有行为不变
        cands = sorted(pool.values(), key=lambda c: -c["rrf_score"])[: cand_limit * 2]
        best_by_source, seen_src = {}, set()
        for c in cands:
            src = c["meta"].get("source", "")
            if src in seen_src:
                continue
            has_kw = any(k and k in c["text"] for k in keywords)
            c["_has_kw"] = has_kw
            # 每文件只保留最佳chunk（含查询关键词的chunk优先，其次按 RRF 分）
            best_by_source[src] = c
            seen_src.add(src)
        dedup = sorted(best_by_source.values(),
                       key=lambda c: (not c["_has_kw"], -c["rrf_score"]))[:cand_limit]

        # ---------- Step 4.5: 递进式时间过滤（3月→6月→1年） ----------
        now = time.time()
        note = None
        if time_filter and time_filter != "all":
            days_map = {"3m": 90, "6m": 180, "1y": 365}
            days_map.setdefault(time_filter, 90)
            start_idx = TIME_FILTER_LADDER.index(time_filter) if time_filter in TIME_FILTER_LADDER else 0
            keep = []
            for lvl in TIME_FILTER_LADDER[start_idx:]:
                days = days_map[lvl]
                keep = [c for c in dedup if now - float(c["meta"].get("file_mtime", 0)) <= days * 86400]
                if keep:
                    break
            if keep:
                dedup = keep
            else:
                # 全部候选超过 1 年 → 需用户确认（confirm_old）
                if confirm_old:
                    note = "结果包含一年以上的旧文档（已确认包含）"
                else:
                    old_n = len(dedup)
                    note = f"有 {old_n} 条一年以上的旧文档未展示，可设置 confirm_old=true 包含"
                    dedup = []
        dedup = dedup[: max(10, top_k)]

        # ---------- Step 5: 三档重排序（6.4） ----------
        max_vec = max((c["vec_sim"] for c in dedup), default=0.0)
        if not rerank or max_vec >= SKIPPED_THRESHOLD:
            rerank_mode = "skipped"
            dedup.sort(key=lambda c: -c["rrf_score"])
            out = [c for c in dedup if c["vec_sim"] >= NORERANK_VEC_FILTER]
            for c in out:
                c["rerank_score"] = c["rrf_score"]
        elif max_vec >= LIGHTWEIGHT_THRESHOLD:
            rerank_mode = "lightweight"
            # V1.7.10：归一化基准排除 BM25-only boost 候选——其 rrf 含 +10 人为权重，
            # 若作为基准会把真实候选的归一化分压扁到近 0（实测发言稿 rrf_norm=0.004），
            # 导致向量强命中反而排在 boost 文档之后。boost 候选自身维持满分归一化
            # （其排序地位不变，评估 top5 组成与基线一致）；非 boost 候选之间除数一致、
            # 相对序不变，只是相对 boost 上移到真实证据对应的位置。
            top_rrf = max((c["rrf_score"] for c in dedup if not c.get("_bm25_boosted")), default=1.0) or 1.0
            for c in dedup:
                fname_score = self._fname_score(c["meta"], fname_kws)
                rrf_norm = 1.0 if c.get("_bm25_boosted") else (c["rrf_score"] / top_rrf)
                c["rerank_score"] = (LW_W_VEC * c["vec_sim"]
                                     + LW_W_RRF * rrf_norm
                                     + LW_W_FNAME * fname_score
                                     + LW_W_TIME * self._freshness(c["meta"]))   # V1.8.5：时效软加权
            out = [c for c in dedup if c["rerank_score"] >= LIGHTWEIGHT_FILTER_THRESHOLD]
            out.sort(key=lambda c: -c["rerank_score"])
        else:
            rerank_mode = "full"
            dedup.sort(key=lambda c: -c["rrf_score"])
            head, tail = dedup[:RERANK_TOP_N], dedup[RERANK_TOP_N:]
            if head:
                pairs = [(query, c["text"][:1500]) for c in head]
                scores = self.reranker.predict(pairs)
                for c, s in zip(head, scores):
                    bonus = (self._fname_score(c["meta"], fname_kws) * RERANK_FNAME_BONUS  # 文件名匹配加成(≤+0.1)
                             + RERANK_TIME_BONUS * self._freshness(c["meta"]))              # V1.8.5：时间加成(≤+0.05)
                    c["rerank_score"] = float(s) + bonus
                del pairs, scores  # 推理结果即时释放（8.5）
            for c in tail:
                c["rerank_score"] = c["rrf_score"]  # 未参与推理的候选用RRF分数兜底
            out = [c for c in dedup if c["rerank_score"] >= FULL_FILTER_THRESHOLD]
            out.sort(key=lambda c: -c["rerank_score"])

        # ---------- Step 6: 上下文组装（6.5） ----------
        results = out[:top_k]
        # Step 6.5: 最低相关性过滤（6.6）
        low_relevance = False
        if results:
            top_rel = max(results[0]["rerank_score"], results[0]["vec_sim"])
            if top_rel < MIN_RELEVANCE:
                low_relevance = True
                results = []
        if results and expand_context:
            for c in results:
                c["context"] = self._assemble_context(c, keywords)
        return self._finalize(query, results,
                              {"queries": queries, "intent": intent, "syn_fusion": syn_for_fusion},
                              t_start, time_filter, rerank_mode, note, low_relevance)

    # ---------- 辅助方法 ----------
    @staticmethod
    def _freshness(meta):
        """V1.8.5：新鲜度分 = 0.5^(文档年龄天数/半衰期)，∈[0,1]。
        mtime 缺失取中性 0.5（不误伤无时间字段的候选）；未来时间截断为 1.0。"""
        try:
            mt = float(meta.get("file_mtime") or 0)
        except (TypeError, ValueError):
            mt = 0.0
        if mt <= 0:
            return 0.5
        age_days = max(0.0, (time.time() - mt) / 86400.0)
        return 0.5 ** (age_days / TIME_DECAY_HALF_LIFE_DAYS)

    @staticmethod
    def _fname_score(meta, fname_kws):
        """文件名匹配得分：filename×0.7 + directory×0.3（6.4）"""
        if not fname_kws:
            return 0.0
        fn, td = meta.get("filename", ""), meta.get("top_dir", "")
        fn_hit = sum(1 for k in fname_kws if k and k in fn) / len(fname_kws)
        dir_hit = (sum(1 for k in fname_kws if k and k in td) / len(fname_kws)) if td else 0.0
        return fn_hit * 0.7 + dir_hit * 0.3

    def _assemble_context(self, cand, keywords):
        """上下文组装：前一chunk尾部300 + 当前全量 + 后一chunk头部400 + 跨文件补充（≤2个文件）"""
        src = cand["meta"].get("source", "")
        chunks = self.ctx_by_source.get(src, [])
        idx = cand["meta"].get("chunk_idx", 0)
        prev = next((c for c in chunks if c.get("chunk_idx") == idx - 1), None)
        nxt = next((c for c in chunks if c.get("chunk_idx") == idx + 1), None)
        parts = []
        if prev:
            parts.append(prev["text"][-CTX_PREV_TAIL:])
        parts.append(cand["text"])
        if nxt:
            parts.append(nxt["text"][:CTX_NEXT_HEAD])
        ctx = "\n".join(parts)
        # 跨文件同类补充：同目录下含查询关键词的其他文件片段（关键词前后各150字）
        top_dir = cand["meta"].get("top_dir", "")
        used_files = 0
        for other_src, other_chunks in self.ctx_by_dir.get(top_dir, {}).items():
            if used_files >= CROSS_FILE_MAX_FILES:
                break
            if other_src == src:
                continue
            for oc in other_chunks:
                pos = -1
                for k in keywords:
                    pos = oc["text"].find(k)
                    if pos >= 0:
                        break
                if pos >= 0:
                    lo, hi = max(0, pos - CROSS_FILE_SNIPPET), pos + CROSS_FILE_SNIPPET
                    ctx += f"\n\n[关联片段|{oc.get('filename', '')}] {oc['text'][lo:hi]}"
                    used_files += 1
                    break
        return ctx[:CTX_MAX_LEN]

    def _finalize(self, query, results, extra, t_start, time_filter, rerank_mode, note, low_relevance=False):
        elapsed = (time.time() - t_start) * 1000
        self.query_count += 1
        self.latency_sum += elapsed
        if self.query_count % 10 == 0:  # 8.5 定期GC：每10次查询
            gc.collect()
        out = {
            "query": query,
            "rewritten_queries": extra["queries"],
            "intent": extra["intent"],
            "rerank_mode": rerank_mode,
            "time_filter": time_filter,
            "elapsed_ms": round(elapsed, 1),
            "low_relevance": low_relevance,
            "results": [
                {
                    "source": c["meta"].get("source", ""),
                    "filename": c["meta"].get("filename", ""),
                    "top_dir": c["meta"].get("top_dir", ""),
                    "chunk_idx": c["meta"].get("chunk_idx", 0),
                    "chunk_type": c["meta"].get("chunk_type", ""),
                    "file_type": c["meta"].get("file_type", ""),
                    "file_mtime": c["meta"].get("file_mtime", 0.0),
                    "vec_sim": round(c["vec_sim"], 4),
                    "rrf_score": round(c["rrf_score"], 4),
                    "rerank_score": round(c.get("rerank_score", 0.0), 4),
                    "text": c["text"],
                    "context": c.get("context", ""),
                }
                for c in results
            ],
        }
        if note:
            out["time_filter_note"] = note
        return out

    # ---------- /health /stats /eval（8.2） ----------
    def _safe_chunk_count(self) -> int:
        """安全计数：全量重建删除集合的瞬间其他请求可能撞到失效引用，
        异常时回退上次成功值（避免竞态500）"""
        try:
            n = self.collection.count()
            self._last_chunks = n
            return n
        except Exception:  # noqa: BLE001
            return getattr(self, "_last_chunks", 0)

    def health(self):
        import psutil
        bm25_ok = bool(self.bm25_data and self.bm25_data.get("doc_ids"))
        return {
            "status": "ok",
            "chunks": self._safe_chunk_count(),
            "bm25_loaded": bm25_ok,
            "bm25_docs": self.bm25_data.get("total_docs", 0) if bm25_ok else 0,
            "context_sources": len(self.ctx_by_source),
            "models_loaded": self._models_loaded,
            "memory_mb": round(psutil.Process().memory_info().rss / 1024 / 1024, 1),
            "uptime_sec": round(time.time() - self.started_at, 0),
        }

    def stats(self):
        import psutil
        return {
            "query_count": self.query_count,
            "avg_latency_ms": round(self.latency_sum / self.query_count, 1) if self.query_count else 0,
            "uptime_sec": round(time.time() - self.started_at, 0),
            "memory_mb": round(psutil.Process().memory_info().rss / 1024 / 1024, 1),
            "chunks": self._safe_chunk_count() if hasattr(self, "collection") else 0,
        }

    def run_eval(self):
        """评估框架（10.4）：32项用例，绕过时间过滤；每5个用例GC一次"""
        hits, top1_hits, latencies, details = 0, 0, [], []
        for i, (q, expected) in enumerate(EVAL_CASES):
            t0 = time.time()
            r = self.search(q, top_k=5, time_filter="all", rerank=True, expand_context=False)
            latencies.append((time.time() - t0) * 1000)
            res = r["results"]
            hit = any(
                expected in c["filename"] or (c["top_dir"] and expected in c["top_dir"])
                or expected in c["text"]
                for c in res
            )
            top1 = bool(res) and expected in res[0]["filename"]
            hits += 1 if hit else 0
            top1_hits += 1 if top1 else 0
            details.append({"query": q, "expected": expected, "hit": hit,
                            "top1": top1, "n_results": len(res),
                            "top1_file": res[0]["filename"] if res else ""})
            if (i + 1) % 5 == 0:
                gc.collect()
        gc.collect()  # 评估后GC（8.5）
        total = len(EVAL_CASES)
        return {
            "total": total,
            "hit_rate": round(hits / total, 4),
            "top1_rate": round(top1_hits / total, 4),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "missed": [d for d in details if not d["hit"]],
            "details": details,
        }
