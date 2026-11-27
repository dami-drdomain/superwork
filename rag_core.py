# -*- coding: utf-8 -*-
"""
RAG 知识库系统 —— 核心模块（文档处理层）
按《RAG知识库系统关键技术说明书 v3.0》实现：
  - 全局配置（对齐说明书十五、关键技术参数速查表）
  - 噪音文件过滤 / 格式路由与真实格式检测（第二章）
  - 文本噪音清理 _clean_noise_text（2.3）
  - 多格式文本提取，11 种格式（2.1）
  - 结构感知分块，四类块类型（第三章）
  - jieba 自定义词典初始化（5.3）
"""
import os
import re
import json
import time
import hashlib
import logging

logger = logging.getLogger("rag_kb")

# ============================================================
# 全局配置（速查表：所有关键参数集中此处，便于按 13.5 定制化调整）
# ============================================================
KB_ROOT = os.path.dirname(os.path.abspath(__file__))

def _docs_dir():
    """知识库文档目录：优先读环境变量 RAG_DOCS_DIR，默认 rag-kb/docs/"""
    d = os.environ.get("RAG_DOCS_DIR")
    return d if d else os.path.join(KB_ROOT, "docs")

DOCS_DIR = _docs_dir()
DB_DIR = os.path.join(DOCS_DIR, ".rag_db_embed")          # ChromaDB 存储路径（4.2）
STATE_FILE = os.path.join(DB_DIR, ".rag_state_embed.json")  # 构建状态文件（12.2）
BM25_FILE = os.path.join(DB_DIR, "bm25_index.pkl")          # BM25 索引（5.2）
TEXT_CACHE_FILE = os.path.join(DB_DIR, ".rag_text_cache.json")  # 全文本缓存（11.1）
SYNC_STATE_FILE = os.path.join(DOCS_DIR, ".sync_state.json")   # 同步状态文件（12.2）
VERIFY_SNAPSHOT_FILE = os.path.join(DB_DIR, ".rag_verify_snapshot.json")  # 验证快照（12.2）

CHROMA_COLLECTION = "wps_docs"          # 集合名称（4.2）
LOCAL_MODEL_DIR = os.path.join(KB_ROOT, "models")  # 本地模型目录（13.3 离线拷贝模式）
MODEL_CONFIG_FILE = os.path.join(KB_ROOT, "models_config.json")  # 模型配置文件（App配置界面读写）
DEFAULT_EMBED_MODEL = "BAAI/bge-base-zh-v1.5"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


def load_model_config() -> dict:
    """读取模型配置文件（用户可在 App 配置界面修改）"""
    try:
        with open(MODEL_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_model_config(cfg: dict):
    """保存模型配置文件"""
    with open(MODEL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def _resolve_model(name: str) -> str:
    """模型名解析：本地 models/ 目录存在同名目录则直接本地加载（完全离线）"""
    if not name:
        return name
    if os.path.isabs(name):
        return name
    local = os.path.join(LOCAL_MODEL_DIR, name.split("/")[-1])
    if os.path.isdir(local) and os.path.exists(os.path.join(local, "config.json")):
        return local
    return name


def reload_model_config() -> dict:
    """
    重读模型配置并更新模块级 EMBED_MODEL / RERANK_MODEL。
    优先级：models_config.json > 环境变量 > 默认模型
    """
    global EMBED_MODEL, RERANK_MODEL
    cfg = load_model_config()
    embed = cfg.get("embed_model") or os.environ.get("RAG_EMBED_MODEL") or DEFAULT_EMBED_MODEL
    rerank = cfg.get("reranker_model") or os.environ.get("RAG_RERANK_MODEL") or DEFAULT_RERANK_MODEL
    EMBED_MODEL = _resolve_model(embed)
    RERANK_MODEL = _resolve_model(rerank)
    return {"embed_model": EMBED_MODEL, "reranker_model": RERANK_MODEL,
            "embed_configured": embed, "reranker_configured": rerank}


EMBED_MODEL = DEFAULT_EMBED_MODEL    # 嵌入模型（4.1，将被 reload_model_config 按配置覆盖）
RERANK_MODEL = DEFAULT_RERANK_MODEL  # 重排序模型（6.4）
reload_model_config()                # import 时立即应用一次当前配置
VECTOR_DIM = 768                          # 向量维度
EMBED_BATCH_SIZE = 64                     # 编码批处理大小
CHROMA_WRITE_BATCH = 500                  # 向量分批写入（4.3）

# ---------- 文本提取限制（2.1 / 速查表） ----------
MAX_FILE_SIZE = 20 * 1024 * 1024      # 单文件上限 20MB（通用，V1.6.3 由 10MB 提升）
MAX_FILE_SIZE_PPT = 200 * 1024 * 1024  # v5.3.4 100MB → V1.7.1 放宽到 200MB——
# PPT 体积主要是图片/媒体资源，文本量很小（实测 62MB PPT 仅 1.5 万字符），
# 过小上限会造成大 PPT 无法收集入库的误伤
MAX_TEXT_CHARS = 500_000              # 单文件最大文本 50万字符
PDF_MAX_PAGES = 50                    # PDF 前 50 页
EXCEL_MAX_ROWS = 300                  # Excel 每表 300 行

# ---------- 分块参数（3.1 / 速查表） ----------
MAX_LEN = 600            # 单块最大字符数
MIN_LEN = 30             # 单块最小有效字符数
OVERLAP = 50             # 段落类型块尾部重叠
MAX_CHUNKS_PER_FILE = 500    # 单文件最大分块数
MIN_FILE_TEXT = 50           # 低于此长度的文件跳过分块

# ---------- 检索参数（第六章 / 速查表） ----------
RRF_K = 60                          # RRF k 值
SUBQUERY_WEIGHT = 0.6               # 扩展子查询权重（主查询 1.0）
FILENAME_MATCH_BOOST = 0.008        # 文件名匹配 boost
BM25_ONLY_BOOST = 10.0              # BM25-only boost
BM25_ONLY_BOOST_MAX = 2             # v5.2.3：每查询最多加权的 BM25-only 候选数
                                     # （10万+语料下不设上限会碾压向量路全部真实命中）
BM25_ONLY_LIGHTWEIGHT_FLOOR = 0.15  # BM25 低向量补偿
SKIPPED_THRESHOLD = 0.75           # 三档重排序：skipped 档阈值
LIGHTWEIGHT_THRESHOLD = 0.55       # 三档重排序：lightweight 档阈值
FULL_FILTER_THRESHOLD = 0.02       # full 档低质量过滤
LIGHTWEIGHT_FILTER_THRESHOLD = 0.03  # lightweight 档低质量过滤
NORERANK_VEC_FILTER = 0.12         # 无重排序档 vec_sim 兜底过滤
MIN_RELEVANCE = 0.3                # 最低相关性阈值（Top-1 < 0.3 判 low_relevance）
RERANK_TOP_N = 8                   # full 档 CrossEncoder 只对前 8 个候选推理
RERANK_FNAME_BONUS = 0.1           # full 档文件名匹配加成上限
LW_W_VEC, LW_W_RRF, LW_W_FNAME = 0.35, 0.55, 0.10  # lightweight 混合评分权重
# V1.8.5：时效软加权——新鲜度 = 0.5^(文档年龄天数/半衰期)，只在相近分数间决胜
LW_W_TIME = 0.10                # 轻量档新鲜度权重
RERANK_TIME_BONUS = 0.05        # 重排档时间加成上限（与文件名加成同量级）
TIME_DECAY_HALF_LIFE_DAYS = 180 # 新鲜度半衰期（天）
SYN_VEC_FUSION_WEIGHT = 0.35        # 同义词向量融合权重（6.3）
SYN_VEC_FUSION_MAX = 2              # 同义词向量融合上限（最多 2 个）
MAX_SYNONYM_GROUPS = 3               # 同义词扩展最多 3 组（7.1）
MAX_SUBQUERIES = 5                   # 复合查询拆解子查询上限（7.3）
CANDIDATE_LIMIT = 30                 # 融合后进入重排序的候选上限

# ---------- 上下文组装（6.5 / 速查表） ----------
CTX_PREV_TAIL = 300            # 前一 chunk 尾部 300 字符
CTX_NEXT_HEAD = 400            # 后一 chunk 头部 400 字符
CTX_MAX_LEN = 2500             # 组装后文本上限
CROSS_FILE_MAX_FILES = 2       # 跨文件补充最大文件数
CROSS_FILE_SNIPPET = 150       # 跨文件片段关键词前后各 150 字

# ---------- 时间过滤（6.1 Step4.5 / 速查表） ----------
TIME_FILTER_DEFAULT = "3m"     # 默认 3 个月
TIME_FILTER_LADDER = ["3m", "6m", "1y"]  # 递进放宽序列

# ---------- 服务配置（第八章） ----------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("RAG_SERVER_PORT", "18800"))
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}

# ============================================================
# 噪音文件过滤（2.4）
# ============================================================
NOISE_FILENAME_PATTERNS = [
    r"^\.sync_state\.json$", r"^\.rag_state", r"^Thumbs\.db$", r"^~\$",
    r"\.tmp$", r"\.bak$", r"^\.DS_Store$", r"^desktop\.ini$",
]
NOISE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico", ".svg",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".dmg", ".iso",
    ".sqlite3", ".db", ".pyc", ".pkl", ".exe", ".dll", ".bin",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
}
EXCLUDE_DIRS = {".rag_db_embed", "__pycache__", ".git", ".venv", "node_modules", ".temp"}
SUPPORTED_EXTS = {
    ".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx",
    ".md", ".txt", ".csv", ".html", ".htm",
}


def is_noise_file(path: str, name: str) -> bool:
    """噪音文件过滤：黑名单文件名 + 噪音后缀（2.4）"""
    for pat in NOISE_FILENAME_PATTERNS:
        if re.search(pat, name, re.IGNORECASE):
            return True
    if os.path.splitext(name)[1].lower() in NOISE_EXTS:
        return True
    return False


def max_size_for(name: str) -> int:
    """按格式返回单文件大小上限（v5.3.4：ppt/pptx 放宽到 100MB）"""
    ext = os.path.splitext(name)[1].lower()
    return MAX_FILE_SIZE_PPT if ext in (".ppt", ".pptx") else MAX_FILE_SIZE


def scan_files(docs_dir: str = None):
    """递归扫描文档目录，返回受支持格式的文件相对路径列表（排除噪音文件与目录）"""
    docs_dir = docs_dir or DOCS_DIR
    files = []
    _size_warned = set()   # 同一超大文件只警告一次（避免轮询场景日志刷屏）
    for root, dirs, names in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".rag_")]
        for name in names:
            if is_noise_file(name, name):
                continue
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTS:
                continue
            full = os.path.join(root, name)
            try:
                size_limit = max_size_for(name)
                if os.path.getsize(full) > size_limit:
                    rel = os.path.relpath(full, docs_dir)
                    if rel not in _size_warned:
                        _size_warned.add(rel)
                        logger.warning("跳过超大文件(>%dMB): %s", size_limit // 1024 // 1024, full)
                    continue
            except OSError:
                continue
            files.append(os.path.relpath(full, docs_dir))
    return sorted(files)


def file_md5(path: str) -> str:
    """计算文件 MD5（增量更新机制 4.4 的哈希比对基础）"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ============================================================
# 文本噪音清理（2.3）—— 四级清理机制
# ============================================================
_RE_MD_BASE64_IMG = re.compile(r"!\[[^\]]*\]\(\s*data:image/[^)]*\)")
_RE_BARE_DATA_URI = re.compile(r"data:image/[a-zA-Z0-9+./=;-]+;base64,[A-Za-z0-9+/=]+")
_RE_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{200,}(?![A-Za-z0-9+/=])")
_RE_PURE_BASE64_LINE = re.compile(r"^[A-Za-z0-9+/=]{500,}$")
_RE_BLANK_LINES = re.compile(r"\n{3,}")


def _clean_noise_text(text: str) -> str:
    """四级噪音清理：base64 图片/裸Data URI/超长base64串/超长行，并压缩连续空行"""
    text = _RE_MD_BASE64_IMG.sub("", text)
    text = _RE_BARE_DATA_URI.sub("", text)
    text = _RE_LONG_BASE64.sub("", text)
    kept = []
    for line in text.split("\n"):
        if _RE_PURE_BASE64_LINE.match(line.strip()):
            continue  # 纯 base64 行直接丢弃
        if len(line) > 10000:
            line = line[:10000]  # 超长行截断
        kept.append(line)
    text = "\n".join(kept)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


# ============================================================
# v4.8.1 乱码门闸：可读字符占比过低（OLE 旧格式误读/二进制兑底文本）→ 跳过编码入库
# （实测 1.4MB .doc 兑底文本产生 500 个乱码 chunks，编码耗时 8.6 分钟且污染检索索引）
# ============================================================
_RE_READABLE = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2000-\u206fa-zA-Z0-9\s,.!?;:()\[\]{}<>|/@#$%^&*+=~`'\"]")


def _readable_ratio(text: str) -> float:
    """可读字符占比：中文/CJK标点/全角符号/通用标点/ASCII字母数字/空白"""
    if not text:
        return 0.0
    return len(_RE_READABLE.findall(text)) / len(text)


# v1.5.9：PDF 原始二进制兜底检测——扫描件提取失败时会把 "%PDF-1.3 /FlateDecode" 当正文，
# 可读率很高（全是 ASCII）却完全无意义，实测一份 9MB 证书 PDF 因此产出 500 个垃圾 chunk。
_RE_PDF_SIG = re.compile(r"%PDF-\d\.\d|/Filter\s*/FlateDecode|\bendstream\b|\b\d+\s+0\s+obj\b")


def looks_like_pdf_binary(text: str) -> bool:
    """正文是否为未解析的 PDF 二进制（提取失败兜底）"""
    head = text[:4000].lstrip()
    if head.startswith("%PDF-"):
        return True
    return len(_RE_PDF_SIG.findall(text[:20000])) >= 3


# ============================================================
# 格式路由与真实格式检测（2.2）—— 双层检测 + 文本兜底
# ============================================================
def detect_real_format(path: str) -> str:
    """读取文件头部 8 字节，判断真实格式"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"   # Office 2007+（docx/xlsx/pptx）
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"   # Office 97-2003（doc/xls/ppt）
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary"


def _try_text_fallback(path: str) -> str:
    """兜底策略：解析失败/格式不匹配时回退为 UTF-8 文本读取，确保可读文件不被遗漏"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(MAX_TEXT_CHARS)
    except OSError:
        return ""


def extract_doc_ole(path: str) -> str:
    """OLE 旧格式 .doc：python-docx 无法解析，改用 macOS 原生 textutil 提取。

    v5.2.9：旧版路由到 extract_docx 解析失败后回退 UTF-8 读到 OLE 二进制乱码，
    全数被乱码门闸拦截，38 个真实业务文档（评审方案/验收证书/委托函等）无法入库。
    实测 textutil 可完整提取正文，且无需额外依赖。非 macOS 或 textutil 失败时仍回退旧策略。
    """
    import subprocess
    try:
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", path],
                           capture_output=True, timeout=60)
        text = r.stdout.decode("utf-8", errors="ignore")
        if text.strip():
            return text
    except Exception:  # noqa: BLE001
        pass
    return _try_text_fallback(path)


# ============================================================
# 各格式解析器（2.1 支持的文档格式与解析策略）
# ============================================================
def extract_docx(path: str) -> str:
    """Word：段落文本 + 表格内容（行以 | 分隔）"""
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def extract_pdf(path: str) -> str:
    """PDF：逐页提取文本（前 50 页）"""
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:PDF_MAX_PAGES]:
            out.append(page.extract_text() or "")
    return "\n\n".join(out)


def extract_xlsx(path: str) -> str:
    """Excel：逐工作表提取，行以 | 分隔（每表 300 行）"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"# 工作表: {ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= EXCEL_MAX_ROWS:
                break
            out.append(" | ".join("" if c is None else str(c) for c in row))
        out.append("")
    wb.close()
    return "\n".join(out)


def extract_xls(path: str) -> str:
    """Excel 旧版 .xls：逐工作表提取（每表 300 行）"""
    import xlrd
    wb = xlrd.open_workbook(path)
    out = []
    for ws in wb.sheets():
        out.append(f"# 工作表: {ws.name}")
        for r in range(min(ws.nrows, EXCEL_MAX_ROWS)):
            out.append(" | ".join(str(c.value) if c.value is not None else "" for c in ws.row(r)))
        out.append("")
    return "\n".join(out)


def extract_pptx(path: str) -> str:
    """PPT：逐幻灯片文本框提取（全部幻灯片）"""
    from pptx import Presentation
    prs = Presentation(path)
    out = []
    for idx, slide in enumerate(prs.slides, 1):
        out.append(f"# 幻灯片 {idx}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs)
                    if t.strip():
                        out.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    out.append(" | ".join(c.text for c in row.cells))
        out.append("")
    return "\n\n".join(out)


def extract_ppt(path: str) -> str:
    """PPT 旧版 .ppt（OLE 二进制）：扫描文本原子记录提取正文（V1.7.17）。

    PowerPoint 97-2003 无现成解析库（python-pptx 仅支持 pptx）。按 MS-PPT 记录结构
    定位正文文本：TextCharsAtom(0x0FA0, UTF-16LE) 与 TextBytesAtom(0x0FA8, 单字节编码)，
    以字节模式快速定位记录头（20MB 文件毫秒级），过滤母版占位文字并去重保序。
    仅用于「选择样例」的文本提取，不加入索引扫描格式清单。
    """
    import re as _re
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:  # noqa: BLE001
        return ""
    runs, seen = [], set()
    pos, n = 0, len(data)
    while pos + 8 <= n:
        j1 = data.find(b"\xa0\x0f", pos)          # TextCharsAtom 记录类型 0x0FA0（小端）
        j2 = data.find(b"\xa8\x0f", pos)          # TextBytesAtom 记录类型 0x0FA8
        cands = [j for j in (j1, j2) if j >= 2]
        if not cands:
            break
        j = min(cands)
        rec_type = 0x0FA0 if j == j1 else 0x0FA8
        start = j - 2
        ver_inst = int.from_bytes(data[start:start + 2], "little")
        rec_len = int.from_bytes(data[start + 4:start + 8], "little")
        if (ver_inst & 0x000F) == 0 and 0 < rec_len <= 1000000 and start + 8 + rec_len <= n:
            payload = data[start + 8:start + 8 + rec_len]
            if rec_type == 0x0FA0:
                text = payload.decode("utf-16-le", errors="ignore")
            else:
                text = ""
                for enc in ("utf-8", "gb18030", "cp1252"):
                    cand = payload.decode(enc, errors="ignore")
                    if cand and "\ufffd" not in cand:
                        text = cand
                        break
            text = _re.sub(r"[\x00-\x08\x0e-\x1f]", "",
                           text.replace("\r", "\n").replace("\x0b", "\n")).strip()
            if len(text) >= 2 and text not in seen:
                ok = sum(1 for c in text if c.isprintable() or c in "\n\t") / len(text)
                if ok >= 0.65:
                    seen.add(text)
                    runs.append(text)
            pos = start + 8 + rec_len
        else:
            pos = j + 1
    # 母版占位提示（模板自带，非正文）不进入样例骨架
    drop = ("click to edit master", "单击此处编辑母版", "second level", "third level",
            "fourth level", "fifth level", "第二级", "第三级", "第四级", "第五级")
    lines = []
    for line in "\n".join(runs).split("\n"):
        s = line.strip()
        if not s or s == "*" or s.lower().startswith(drop):
            continue
        lines.append(s)
    return "\n".join(lines)


def extract_text_file(path: str) -> str:
    """Markdown / 纯文本 / CSV / HTML：直接 UTF-8 原文读取"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read(MAX_TEXT_CHARS)


# 扩展名 → 解析器路由表（第一层：扩展名路由）
_EXT_PARSERS = {
    ".docx": extract_docx,
    ".doc": extract_doc_ole,   # v5.2.9：OLE 旧格式改走 textutil（旧版 python-docx 解析失败回退读乱码，38 个文档全被门闸拦截）
    ".pdf": extract_pdf,
    ".xlsx": extract_xlsx,
    ".xls": extract_xls,
    ".pptx": extract_pptx,
    ".ppt": extract_ppt,       # V1.7.17：旧版 PPT 二进制，供模版仿写提取正文
    ".md": extract_text_file,
    ".txt": extract_text_file,
    ".csv": extract_text_file,
    ".html": extract_text_file,
    ".htm": extract_text_file,
}

# 扩展名 → Magic Bytes 期望格式（第二层：真实格式检测）
_EXT_EXPECT = {
    ".pdf": "pdf", ".docx": "zip", ".xlsx": "zip", ".pptx": "zip",
    ".doc": "ole", ".xls": "ole",
}


def extract_file(path: str) -> str:
    """格式路由提取入口：扩展名路由 → Magic Bytes 校验 → 解析 → 失败回退 UTF-8"""
    ext = os.path.splitext(path)[1].lower()
    parser = _EXT_PARSERS.get(ext)
    if parser is None:
        return ""
    try:
        expected = _EXT_EXPECT.get(ext)
        if expected and detect_real_format(path) not in (expected,):
            # 扩展名与真实格式不符 → 文本兜底
            return _try_text_fallback(path)
        text = parser(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("解析失败，回退文本读取 %s: %s", path, e)
        return _try_text_fallback(path)
    if not text or not text.strip():
        return _try_text_fallback(path)
    return text[:MAX_TEXT_CHARS]


def extract_file_safe(path: str):
    """提取异常兜底版（供并行提取子进程调用；不依赖 torch 等重型库，worker 启动快）"""
    try:
        mtime = os.path.getmtime(path)
        return path, extract_file(path), mtime
    except Exception:  # noqa: BLE001
        return path, "", time.time()


# ============================================================
# 结构感知分块（第三章）
# ============================================================
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])")
_HEADING_RE = re.compile(
    r"^(#{1,6}\s|第[一二三四五六七八九十百千\d]+[章节条部分篇]|[一二三四五六七八九十]+、|\d+(\.\d+)*[、.\s])"
)
_LIST_RE = re.compile(r"^\s*([-*•·]|\d+[.、）)]|[（(]\d+[)）]|[a-zA-Z][.、)])")


def _classify_block(block: str) -> str:
    """四类块类型识别（3.2）：table / heading / list / paragraph"""
    lines = [l for l in block.split("\n") if l.strip()]
    n = len(lines)
    if n >= 2:
        sep_ratio = sum(1 for l in lines if "|" in l) / n
        if sep_ratio > 0.5:
            return "table"
        list_ratio = sum(1 for l in lines if _LIST_RE.match(l)) / n
        if list_ratio > 0.5:
            return "list"
    if not lines:
        return "paragraph"
    first = lines[0].strip()
    if first.startswith("#"):
        return "heading"
    if len(block) < 60 and (_HEADING_RE.match(first) or first.endswith(("：", ":"))):
        # 短行(<60字)匹配标题模式（#开头/第X章/数字编号/冒号结尾）
        return "heading"
    return "paragraph"


def _split_oversize(block: str, btype: str):
    """超大块(>600字)切分（3.3）：句子边界 → 换行 → 硬切；paragraph 尾部重叠 50 字"""
    units = _SENT_SPLIT_RE.split(block)
    pieces, cur = [], ""
    for unit in units:
        if not unit:
            continue
        if len(unit) <= MAX_LEN:
            if len(cur) + len(unit) <= MAX_LEN:
                cur += unit
            else:
                if cur:
                    pieces.append(cur)
                cur = unit
        else:
            # 单句超大 → 按换行切分
            for line in unit.split("\n"):
                if not line:
                    continue
                if len(line) <= MAX_LEN:
                    if len(cur) + len(line) <= MAX_LEN:
                        cur += ("\n" if cur else "") + line
                    else:
                        if cur:
                            pieces.append(cur)
                        cur = line
                else:
                    # 行仍超大 → 硬切
                    if cur:
                        pieces.append(cur)
                        cur = ""
                    for k in range(0, len(line), MAX_LEN):
                        pieces.append(line[k:k + MAX_LEN])
    if cur:
        pieces.append(cur)
    # 段落类型块尾部重叠：后一块开头携带前一块尾部 OVERLAP 字符
    if btype == "paragraph" and OVERLAP > 0:
        overlapped = [pieces[0]] if pieces else []
        for prev, piece in zip(pieces, pieces[1:]):
            tail = prev[-OVERLAP:]
            overlapped.append(tail + piece)
        pieces = overlapped
    return [(p, btype) for p in pieces if p.strip()]


def chunk_text(text: str):
    """
    结构感知分块（3.3 处理流程）：
    原文 → 按空行分割为自然段落 → 四类类型分类 → 超大块按句子边界切分
    → 句子仍超大按换行切分 → 过小块(<30字)合并到上一块 → 返回 [(text, chunk_type)]
    """
    text = text.strip()
    if not text:
        return []
    raw_blocks = re.split(r"\n[ \t]*\n", text)
    classified = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        btype = _classify_block(block)
        if len(block) <= MAX_LEN:
            classified.append((block, btype))
        else:
            classified.extend(_split_oversize(block, btype))

    # 过小块合并到上一块（3.3）
    merged = []
    for piece, btype in classified:
        if merged and len(piece.strip()) < MIN_LEN:
            prev_text, prev_type = merged[-1]
            merged[-1] = (prev_text + "\n" + piece, prev_type)
        else:
            merged.append((piece, btype))

    # 裁剪首块过小无上一块可合并的情况
    result = [(p, t) for p, t in merged if p.strip()]
    return result[:MAX_CHUNKS_PER_FILE]


def build_chunks_for_file(rel_path: str, text: str, mtime: float, build_date: str):
    """对单个文件文本执行分块并附加元数据（3.4 Chunk 元数据结构）"""
    text = _clean_noise_text(text)
    if len(text) < MIN_FILE_TEXT:
        return []
    if _readable_ratio(text) < 0.70:   # v4.8.1 乱码门闸：不可读文本不入库（不浪费编码、不污染索引）
        logger.warning("跳过低可读文本（疑似乱码，格式不支持或提取失败）: %s", rel_path)
        return []
    if looks_like_pdf_binary(text):    # v1.5.9：PDF 原始二进制兜底文本同样不入库
        logger.warning("跳过未解析的 PDF 二进制文本（疑似扫描件/提取失败）: %s", rel_path)
        return []
    pieces = chunk_text(text)
    if not pieces:
        return []
    parts = rel_path.replace("\\", "/").split("/")
    top_dir = parts[0] if len(parts) > 1 else ""
    filename = parts[-1]
    file_type = os.path.splitext(filename)[1].lower().lstrip(".")
    chunks = []
    for idx, (piece, btype) in enumerate(pieces):
        chunks.append({
            "id": hashlib.md5(f"{rel_path}_c{idx}".encode("utf-8")).hexdigest()[:16],  # 4.3 ID生成策略
            "text": piece,
            "metadata": {
                "source": rel_path,
                "chunk_idx": idx,
                "chunk_type": btype,
                "file_type": file_type,
                "top_dir": top_dir,
                "filename": filename,
                "build_date": build_date,
                "file_mtime": float(mtime),
            },
        })
    return chunks


# ============================================================
# jieba 分词 + 自定义词典（5.3）
# ============================================================
_JIEBA_CUSTOM_WORDS = [
    "检索增强生成", "大语言模型", "知识库系统", "混合检索", "重排序",
    "向量数据库", "语义检索", "查询改写", "同义词扩展", "上下文组装",
    "增量构建", "全量重建", "守护进程", "应急预案", "网络安全",
    "数据治理", "外包管理", "外包治理", "人工智能应用", "项目验收",
    "故障演练", "责任分工", "评分标准", "台账管理", "知识沉淀",
]
_jieba_ready = False


def init_jieba():
    """加载 jieba 自定义词典，确保领域术语分词时保持完整"""
    global _jieba_ready
    import jieba
    if not _jieba_ready:
        for w in _JIEBA_CUSTOM_WORDS:
            jieba.add_word(w)
        _jieba_ready = True
    return jieba


def tokenize(text: str):
    """jieba 中文分词（BM25 与查询处理共用）"""
    jieba = init_jieba()
    return [t for t in jieba.lcut(text) if t.strip()]
