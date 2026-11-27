# -*- coding: utf-8 -*-
"""
超脑工作搭子 —— 超脑写手模块（v5.0）
  - 大模型接入：OpenAI 兼容 API（base_url / model / api_key 由用户在「模型配置」页自主配置，
    DeepSeek / 通义千问 / 智谱 / Kimi / 本地 Ollama 等均可）
  - 知识库素材引用：复用 App 内 SearchEngine 的检索结果作为写作素材（标注来源）
  - 10 类常用工作文档模板与提示词构建
  - 流式生成（SSE 事件流）、文档保存/列表/读取/删除、Word 导出
依赖：requests（已装）、python-docx（已装）——不引入新依赖
"""
import glob
import json
import logging
import os
import re
import time
from datetime import datetime

import requests

logger = logging.getLogger("rag_kb")

KB_ROOT = os.path.dirname(os.path.abspath(__file__))
LLM_CONFIG_FILE = os.path.join(KB_ROOT, "llm_config.json")
WRITER_DOCS_DIR = os.path.join(KB_ROOT, "writer_docs")
WRITER_INDEX_FILE = os.path.join(WRITER_DOCS_DIR, "index.json")
EXPORTS_DIR = os.path.join(WRITER_DOCS_DIR, "exports")

DEFAULT_LLM_CFG = {
    "base_url": "", "api_key": "", "model": "",
    "temperature": 0.7, "max_tokens": 4096, "timeout": 300,
    # V1.5.8：多模型清单 [{id, alias, model, base_url, api_key, temperature, max_tokens}]；
    # 顶层 base_url/model/api_key 始终与「默认模型」保持同步，供旧代码与旧配置兼容
    "models": [], "default_model_id": "",
    "tiers": {},          # 旧版（V1.5.7）档位配置，仅用于迁移到 models，不再使用
}

TIER_ORDER_LEGACY = ("flagship", "smart", "flash", "lite", "nano")   # V1.5.7 档位顺序（仅用于旧配置迁移）


# ============================================================
# LLM 配置读写
# ============================================================
def _norm_model_entry(item: dict, base: dict, idx: int) -> dict:
    """规范化单个模型条目：未填项继承公共配置，温度/上限用默认值兜底"""
    entry = {
        "id": str(item.get("id") or f"m{idx + 1}").strip(),
        "alias": str(item.get("alias") or "").strip(),
        "model": str(item.get("model") or "").strip(),
        "base_url": str(item.get("base_url") or "").strip(),
        "api_key": str(item.get("api_key") or "").strip(),
        "temperature": item.get("temperature"),
        "max_tokens": item.get("max_tokens"),
    }
    entry["temperature"] = (float(entry["temperature"]) if entry["temperature"] not in (None, "")
                            else float(base.get("temperature") or 0.7))
    entry["max_tokens"] = (int(entry["max_tokens"]) if entry["max_tokens"] not in (None, "")
                           else int(base.get("max_tokens") or 4096))
    return entry


def _migrate_models(cfg: dict) -> list:
    """V1.5.7 及更早配置 → 模型清单。
    v5.x/ V1.5.6：只有单模型公共配置；V1.5.7：另有 tiers 五档（每档可能指定模型）。
    迁移原则：按档位顺序去重收集实际出现的模型；无任何模型则回落到公共配置。
    """
    models = cfg.get("models")
    if isinstance(models, list) and models:
        base = {"temperature": cfg.get("temperature"), "max_tokens": cfg.get("max_tokens")}
        return [_norm_model_entry(m, base, i) for i, m in enumerate(models) if isinstance(m, dict)]

    tiers = cfg.get("tiers") if isinstance(cfg.get("tiers"), dict) else {}
    seen, out = set(), []
    base = {"temperature": cfg.get("temperature"), "max_tokens": cfg.get("max_tokens")}
    for key in TIER_ORDER_LEGACY:
        t = tiers.get(key) if isinstance(tiers.get(key), dict) else {}
        model = str(t.get("model") or cfg.get("model") or "").strip()
        if not model:
            continue
        base_url = str(t.get("base_url") or "").strip()
        sig = (base_url or cfg.get("base_url") or "", model)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(_norm_model_entry({
            "id": f"m{len(out) + 1}", "alias": "", "model": model,
            "base_url": base_url, "api_key": str(t.get("api_key") or ""),
            "temperature": t.get("temperature"), "max_tokens": t.get("max_tokens"),
        }, base, len(out)))
    if out:
        return out
    if not str(cfg.get("model") or "").strip():
        return []
    return [_norm_model_entry({"id": "m1", "model": cfg.get("model"),
                               "base_url": cfg.get("base_url"), "api_key": cfg.get("api_key"),
                               "temperature": cfg.get("temperature"),
                               "max_tokens": cfg.get("max_tokens")}, base, 0)]


def load_llm_config() -> dict:
    try:
        with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            merged = dict(DEFAULT_LLM_CFG)
            merged.update({k: raw[k] for k in DEFAULT_LLM_CFG if k in raw})
            merged["models"] = _migrate_models(merged)
            ids = [m["id"] for m in merged["models"]]
            if merged.get("default_model_id") not in ids:
                merged["default_model_id"] = ids[0] if ids else ""
            return merged
    except (OSError, json.JSONDecodeError):
        pass
    return dict(DEFAULT_LLM_CFG)


def save_llm_config(cfg: dict):
    """保存配置，并保证顶层字段 = 默认模型（旧代码/旧界面仍读顶层字段）"""
    cfg = dict(cfg)
    cfg.pop("tiers", None)          # V1.5.8：迁移完成，旧档位字段不再落盘
    models = cfg.get("models") or []
    default = next((m for m in models if m.get("id") == cfg.get("default_model_id")), None)
    default = default or (models[0] if models else None)
    if default:
        cfg["base_url"] = default.get("base_url") or cfg.get("base_url") or ""
        cfg["model"] = default.get("model") or ""
        cfg["api_key"] = default.get("api_key") or cfg.get("api_key") or ""
        cfg["temperature"] = default.get("temperature", cfg.get("temperature", 0.7))
        cfg["max_tokens"] = default.get("max_tokens", cfg.get("max_tokens", 4096))
        cfg["default_model_id"] = default.get("id", "")
    with open(LLM_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "****" + key[-4:] if len(key) > 8 else "****"


def normalize_base_url(url: str) -> str:
    """接口地址规范化：裸域名自动补 /v1（如 https://api.deepseek.com → …/v1）"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    try:
        host_path = u.split("://", 1)[-1]
        rest = host_path.split("/", 1)
        if len(rest) == 1 or rest[1].strip("/") == "":
            u += "/v1"
    except Exception:  # noqa: BLE001
        pass
    return u


def llm_configured(cfg: dict = None) -> bool:
    cfg = cfg or load_llm_config()
    if cfg.get("models"):
        return any(m.get("model") and (m.get("base_url") or cfg.get("base_url")) for m in cfg["models"])
    return bool(cfg.get("base_url") and cfg.get("model"))


# ============================================================
# V1.5.8：多模型解析（下拉直接显示模型名或用户设置的别名）
# ============================================================
def model_display(entry: dict) -> str:
    """下拉显示名：优先别名，其次模型名"""
    return (entry.get("alias") or "").strip() or (entry.get("model") or "").strip() or "（未命名模型）"


def list_models(cfg: dict = None) -> list:
    """模型清单（脱敏，供界面/下拉使用）"""
    cfg = cfg or load_llm_config()
    out = []
    for m in cfg.get("models") or []:
        eff_base = m.get("base_url") or cfg.get("base_url") or ""
        eff_key = m.get("api_key") or cfg.get("api_key") or ""
        out.append({
            "id": m["id"], "alias": m.get("alias", ""), "model": m.get("model", ""),
            "display": model_display(m), "base_url": eff_base,
            "own_base_url": m.get("base_url", ""), "has_own_key": bool(m.get("api_key")),
            "api_key_masked": mask_key(eff_key),
            "temperature": m.get("temperature"), "max_tokens": m.get("max_tokens"),
            "is_default": m["id"] == cfg.get("default_model_id"),
            "configured": bool(m.get("model") and eff_base),
        })
    if not out and cfg.get("model"):
        out.append({"id": "m1", "alias": "", "model": cfg["model"], "display": cfg["model"],
                    "base_url": cfg.get("base_url", ""), "own_base_url": "",
                    "has_own_key": bool(cfg.get("api_key")),
                    "api_key_masked": mask_key(cfg.get("api_key", "")),
                    "temperature": cfg.get("temperature"), "max_tokens": cfg.get("max_tokens"),
                    "is_default": True, "configured": llm_configured(cfg)})
    return out


def resolve_model(model_id: str = "", cfg: dict = None) -> dict:
    """按模型 id 解析出实际调用配置；id 为空/不存在则用默认模型"""
    cfg = dict(cfg or load_llm_config())
    models = cfg.get("models") or []
    if not models:
        cfg["model_name"] = cfg.get("model", "")
        return cfg
    target = next((m for m in models if m.get("id") == model_id), None)
    target = target or next((m for m in models if m.get("id") == cfg.get("default_model_id")), models[0])
    out = dict(cfg)
    out["base_url"] = target.get("base_url") or cfg.get("base_url") or ""
    out["model"] = target.get("model") or ""
    out["api_key"] = target.get("api_key") or cfg.get("api_key") or ""
    out["temperature"] = target.get("temperature", cfg.get("temperature", 0.7))
    out["max_tokens"] = target.get("max_tokens", cfg.get("max_tokens", 4096))
    out["model_id"] = target.get("id", "")
    out["model_name"] = model_display(target)
    return out


def model_status(model_id: str = "") -> dict:
    """模型可用状态（供写作工作台展示）"""
    cfg = resolve_model(model_id)
    return {"id": cfg.get("model_id", ""), "name": cfg.get("model_name", ""),
            "model": cfg.get("model", ""), "configured": llm_configured(cfg)}


# ============================================================
# 连接测试
# ============================================================
def test_llm(cfg: dict = None, model_id: str = "") -> dict:
    """连接测试：传 model_id 则测试该模型（含其独立接口与 Key）"""
    if model_id:
        cfg = resolve_model(model_id)
    else:
        cfg = cfg or resolve_model()
    if not (cfg.get("base_url") and cfg.get("model")):
        return {"ok": False, "error": "请先填写接口地址与模型名"}
    base = normalize_base_url(cfg["base_url"])
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}"}
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=8)
        if r.status_code == 200:
            try:
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")][:20]
                return {"ok": True, "message": f"连接成功，接口返回 {len(ids)} 个可用模型", "models": ids}
            except ValueError:
                return {"ok": True, "message": "连接成功（接口可达）"}
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"鉴权失败（HTTP {r.status_code}）：请检查 API Key"}
        # 404/405：部分网关不提供 /models，改用 1-token 对话探活
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "连接超时（8 秒）：请检查接口地址是否正确"}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": "无法连接：" + re.sub(r"\s+", " ", str(e))[:150]}
    try:
        rr = requests.post(
            f"{base}/chat/completions",
            headers={**headers, "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1, "stream": False},
            timeout=20)
        if rr.status_code == 200:
            return {"ok": True, "message": "连接成功（对话接口可用）"}
        if rr.status_code in (401, 403):
            return {"ok": False, "error": f"鉴权失败（HTTP {rr.status_code}）：请检查 API Key"}
        return {"ok": False, "error": f"接口返回 HTTP {rr.status_code}：" + rr.text[:150]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "测试失败：" + str(e)[:150]}


# ============================================================
# 10 类文档模板
# ============================================================
DOC_TEMPLATES = {
    "work_summary": {
        "name": "工作总结",
        "outline": "一、总体情况概述；二、主要工作与成效（分点展开，尽量有数据支撑）；三、存在的问题与不足；四、下一步工作打算",
        "tone": "客观务实、以事实和数据说话",
    },
    "periodic_report": {
        "name": "周报/月报",
        "outline": "一、本期工作完成情况（条目式）；二、关键数据与重点进展；三、风险与问题；四、下期工作计划",
        "tone": "简明扼要、条目清晰、突出进度",
    },
    "meeting_minutes": {
        "name": "会议纪要",
        "outline": "会议基本信息（时间/地点/主持人/参会人员）；会议议题；讨论要点（按议题分述）；会议决议；待办事项（明确责任人与完成时限）",
        "tone": "准确、要点式、决议明确",
    },
    "work_plan": {
        "name": "工作方案",
        "outline": "一、背景与现状；二、总体目标；三、总体思路与基本原则；四、重点任务与实施步骤（分阶段，明确责任分工与时间节点）；五、保障措施；六、进度安排",
        "tone": "结构严谨、措施具体、可落地执行",
    },
    "briefing": {
        "name": "汇报材料",
        "outline": "一、总体情况与汇报口径；二、核心数据与亮点成效；三、典型做法或案例；四、存在的困难与请求事项",
        "tone": "条理清晰、重点突出、便于口头汇报",
    },
    "request_report": {
        "name": "请示/报告",
        "outline": "一、请示（报告）缘由；二、具体事项与背景情况；三、建议方案与理由；四、结语（妥否，请批示）",
        "tone": "正式庄重、言简意赅、一文一事",
    },
    "notice": {
        "name": "通知/公告",
        "outline": "主送对象与事由；具体事项与工作要求；时间安排与办理方式；联系方式；落款与日期",
        "tone": "正式规范、表述明确、无歧义",
    },
    "email": {
        "name": "工作邮件",
        "outline": "称呼；背景说明；正文事项（分点表达）；需要的配合或行动（明确时间）；结尾礼貌用语；署名",
        "tone": "专业得体、高效清晰",
    },
    "copywriting": {
        "name": "宣传文案",
        "outline": "吸引人的开头；核心价值主张（分点呈现）；事实与亮点支撑；行动号召",
        "tone": "生动有感染力、突出价值、避免空话",
    },
    "custom": {
        "name": "自定义",
        "outline": "根据主题与要点自行组织最合适的结构",
        "tone": "根据文档用途自行把握",
    },
}

LENGTH_DESC = {
    "short": "篇幅约 300-600 字",
    "medium": "篇幅约 800-1500 字",
    "long": "篇幅约 2000-3500 字",
}
STYLE_DESC = {
    "formal": "正式规范",
    "concise": "简洁干练",
    "vivid": "生动有感染力",
}
SYSTEM_PROMPT = ("你是一位资深的政企办公文书写作专家，擅长撰写规范、专业、结构清晰的中文工作文档，"
                 "写作时严格遵循用户给出的结构要求与事实素材。")

# ============================================================
# V1.5.6：技能（Skill）/ 专家（Expert）/ 工作流 / 模型档位
# 说明：技能对齐 TeleAgent Skill 体系，落为「提示词模板 + 本地产出管线」：
#   doc      → Markdown 成稿，导出 Word（复用既有能力）
#   html     → 单文件 HTML（内嵌 CSS），保存后可预览/在浏览器打开
#   pptx     → 严格 JSON → python-pptx 本地生成可编辑 PPTX
#   xlsx     → 严格 JSON → openpyxl 本地生成 XLSX
#   research → 多轮检索 + 带来源标注的深度报告
# ============================================================
WRITE_SKILLS = {
    "doc": {
        "name": "文档写作", "icon": "📝", "out": "md",
        "desc": "通用工作文档：总结/方案/纪要/请示/通知…（纯文本成稿）",
        "instr": ("【技能：文档写作】产出规范的 Markdown 工作文档：一级标题用「# 」，小节标题用「## 」，"
                  "要点用「- 」或编号列表；直接输出正文，不要输出解释、寒暄或代码块包裹。"),
    },
    "html": {
        "name": "HTML 文档", "icon": "🌐", "out": "html",
        "desc": "排版精美的 HTML 方案/报告（单文件内嵌 CSS，可预览、可用浏览器打开）",
        "instr": ("【技能：html-generator】产出一个完整、可独立打开的 HTML 文档：单文件、内嵌 <style>、"
                  "零外部依赖（不引用 CDN/字体/图片链接）；采用现代化排版（政务/商务风格：清晰的层级、"
                  "克制的配色、表格与卡片排版、打印友好）；只输出 HTML 源码本身，从 <!DOCTYPE html> 开始，"
                  "不要 Markdown 代码块包裹，不要任何解释文字。"),
    },
    "pptx": {
        "name": "PPT 演示", "icon": "📊", "out": "pptx",
        "desc": "设计系统版式引擎：封面/要点/KPI/图表/流程/对比/表格，原生可编辑 PPTX",
        "instr": ("【技能：pptx】只输出严格 JSON（不要 Markdown 代码块、不要解释），按下面的版式规范组织内容：\n"
                  '{"title":"演示标题","subtitle":"副标题/汇报单位","meta":"汇报单位 · 2026年11月","theme":"gov|business|tech|warm|minimal",'
                  '"slides":[{"type":"cover","title":"","subtitle":"","kicker":"汇报材料","meta":""},'
                  '{"type":"section","title":"章节名","subtitle":"一句话本章要回答什么"},'
                  '{"type":"bullets","title":"结论式标题","subtitle":"可选补充","bullets":["要点"]},'
                  '{"type":"kpi","title":"关键指标","items":[{"value":"98%","label":"指标名","note":"口径或对比"}],"insight":"结论"},'
                  '{"type":"chart","title":"趋势","chart_type":"bar|hbar|line|pie|donut","categories":["Q1"],"series":[{"name":"","values":[1]}],"insight":"结论"},'
                  '{"type":"process","title":"实施路径","steps":["第一步"]},{"type":"timeline","title":"推进节奏","events":[{"date":"2026Q1","text":"里程碑"}]},{"type":"layers","title":"总体架构","layers":[{"name":"应用层","desc":"面向用户"}]},{"type":"hub","title":"体系关系","center":"中枢","nodes":[{"name":"节点","note":"职责"}]},{"type":"matrix","title":"四类场景","quadrants":[{"label":"象限","items":["要点"]}]},{"type":"stat","title":"核心结论","value":"18→5分钟","label":"应急响应","note":"口径"},{"type":"quote","text":"关键判断","source":""},'
                  '{"type":"compare","title":"方案对比","left":{"title":"现状","items":[""]},"right":{"title":"目标","items":[""]}},'
                  '{"type":"table","title":"指标表","columns":["指标","目标"],"rows":[["",""]]},'
                  '{"type":"quote","text":"关键判断","source":""},'
                  '{"type":"end","title":"谢谢","subtitle":"敬请批评指正"}]}\n'
                  "内容要求：共 10-14 页；封面 + 2-3 个章节页 + 结尾页；每页只讲一个结论，标题写成判断句（不超过 22 字）；"
                  "**纯要点页（bullets）不得超过内容页的一半，其余内容优先选图形版式**——架构/分层用 layers、演进或计划用 timeline、"
                  "关系或构成用 hub、四象限分类用 matrix、关键数字强调用 stat、有数据用 chart 或 kpi、流程用 process、两案对比用 compare；"
                  "bullets 每页 3-5 条、每条不超过 36 字，用事实/数字/动作，不写空话；有数字时优先用 chart 或 kpi 表达（数据必须来自主题/要点/知识库素材，禁止编造）；"
                  "表格最多 6 列 8 行；theme 按内容选择：政务红金=gov、商务方案=business、科技深色=tech、产品运营=warm、数据分析=minimal；"
                  "每页写 notes（30-80 字讲稿提示），不要复述标题。"),
    },
    "xlsx": {
        "name": "Excel 表格", "icon": "📈", "out": "xlsx",
        "desc": "设计引擎：标题带/斑马纹/公式合计/数据条/自动图表，原生可编辑 XLSX",
        "instr": ("【技能：xlsx】只输出严格 JSON（不要 Markdown 代码块、不要解释），结构："
                  '{"title":"工作簿标题","meta":"单位 · 日期","sheets":[{'
                  '"name":"表名（≤8字）","desc":"一句话说明口径/来源",'
                  '"columns":[{"name":"指标（亿元）","type":"text|int|float|percent","total":true}],'
                  '"rows":[["杭州",123.4,0.23]],"highlight_col":"某数值列名"}]}\n'
                  "内容要求：1-3 张表；每表 4-9 列、6-20 行数据；数值列必须写数字，不要带单位或千分位"
                  "（单位写进列名）；比例列用 percent 类型、小数表示（0.23 表示 23%）；"
                  "需要求和的数值列设 \"total\":true（引擎自动写 =SUM 公式）；"
                  "可指定一个数值列名作为 highlight_col 加数据条；"
                  "数据必须贴合主题、自洽可加总，禁止编造精确到小数点后多位的假数据；"
                  "每张表都要有 desc 说明口径。"),
    },

    "research": {
        "name": "深度研究", "icon": "🔎", "out": "md",
        "desc": "多轮检索知识库，输出带来源引用的深度调研报告",
        "instr": ("【技能：deep-research】产出一份深度研究报告：摘要 → 背景与现状 → 关键发现（分点，尽量给出数据）"
                  " → 趋势与研判 → 建议；引用知识库素材中的事实/数据时，在句末用（来源：文件名）标注；"
                  "素材未覆盖处不得编造数据；Markdown 格式，直接输出正文。"),
    },
}

WRITE_EXPERTS = {
    "general": {"name": "公文写作专家", "icon": "🎓",
                "desc": "规范严谨的政企公文与办公文书",
                "persona": "你是资深的政企办公文书写作专家，擅长撰写规范、专业、结构清晰的中文工作文档，写作时严格遵循用户给出的结构要求与事实素材。"},
    "gov": {"name": "政务方案专家", "icon": "🏛",
            "desc": "数字政府、政务信息化与公共服务方案",
            "persona": "你是深耕数字政府与政务信息化的方案专家，熟悉政务服务、城市治理、民生保障等领域的政策口径与建设模式，产出方案政策依据充分、目标指标清晰、任务举措可落地。"},
    "tech": {"name": "技术方案专家", "icon": "🧩",
             "desc": "系统架构、技术选型与实施方案",
             "persona": "你是资深的解决方案架构师，擅长编写技术方案：架构分层清晰、技术选型有依据、部署与演进路径明确，兼顾安全合规与可运维性。"},
    "bid": {"name": "投标标书专家", "icon": "📑",
            "desc": "招投标响应、商务技术标书",
            "persona": "你精通招投标业务，擅长撰写投标响应文件：逐条响应评分点、突出资质与案例优势、承诺与保障措施具体可信，行文严谨无歧义。"},
    "data": {"name": "数据分析专家", "icon": "📉",
             "desc": "经营分析、数据解读与图表建议",
             "persona": "你是数据分析与经营分析专家，擅长从数据中提炼结论：口径严谨、对比合理、结论与建议一一对应，避免无依据的推断。"},
    "product": {"name": "产品运营专家", "icon": "🚀",
                "desc": "产品规划、运营策略与推广文案",
                "persona": "你是产品与运营专家，擅长产品规划、运营策略与推广材料：用户价值优先、节奏与抓手清晰、文案有感染力且不夸大。"},
}

WORKFLOWS = {
    "standard": {"name": "标准写作", "icon": "⚡", "desc": "检索素材后一次成稿"},
    "outline": {"name": "提纲先行", "icon": "🧭", "desc": "先生成详细提纲，再按提纲成稿（长文更稳）"},
    "template": {"name": "模版仿写", "icon": "📐", "desc": "按所选模版的格式与章节架构仿写"},
}

# ============================================================
# V1.8.0：输出契约——结构化输出的格式要求由引擎统一提供，与技能来源解耦
#   契约与内置技能提示词同源（内置 instr 去掉「【技能：x】」前缀），
#   自定义/固化技能选择 HTML/PPT/Excel 输出时自动追加，提示词只需写领域要求。
# ============================================================
OUT_CONTRACTS = {k: WRITE_SKILLS[k]["instr"].split("】", 1)[1].strip()
                 for k in ("html", "pptx", "xlsx")}
OUT_CONTRACT_MARKS = {                             # 识别提示词是否已自带该格式的契约
    "html": "产出一个完整、可独立打开的 HTML 文档",
    "pptx": "按下面的版式规范组织内容",
    "xlsx": "不要解释），结构：",
}


def _detect_contract(out_kind: str, instr: str) -> str:
    """契约模式：提示词已含格式要求 → embedded；由引擎追加 → auto；无契约 → none"""
    if out_kind not in OUT_CONTRACTS:
        return "none"
    return "embedded" if OUT_CONTRACT_MARKS[out_kind] in (instr or "") else "auto"


# ============================================================
# V1.8.0：三层技能库（v2 存储）
#   builtin 出厂内置（覆盖层可改，可恢复出厂）
#   custom  我的技能（新建/由导入固化，应用自有，不依赖 TeleAgent）
#   imported TeleAgent 导入（SKILL.md 快照，刷新只同步这一层）
#   存储文件：writer_skills.json v2 = {version, updated_at, imported, custom, overrides}
#   兼容：旧 v1 {"skills":[...]} 自动迁移为 imported；写入原子化（tmp+replace）
# ============================================================
SKILL_OUT_KINDS = ("md", "html", "pptx", "xlsx")
SKILL_INSTR_MAX = 20000        # 自定义技能提示词上限
SKILLS_FILE = os.path.join(KB_ROOT, "writer_skills.json")
IMPORTED_SKILLS_FILE = SKILLS_FILE      # 旧名保留兼容
SKILL_BODY_LIMIT = 4000          # 导入技能注入提示词的正文上限（防超长）
SKILL_MD_MAX_BYTES = 400_000     # 单个 SKILL.md 读取上限


def teleagent_skill_dirs() -> list:
    """候选技能目录：环境变量 > 各用户目录 > 全局目录（均只读）"""
    cands = []
    env = (os.environ.get("TELEAGENT_SKILLS_DIR") or "").strip()
    if env:
        cands.append(os.path.expanduser(env))
    base = os.path.expanduser("~/.config/TeleAgent")
    cands.extend(sorted(glob.glob(os.path.join(base, "users", "*", "skills"))))
    cands.append(os.path.join(base, "skills"))
    seen, out = set(), []
    for d in cands:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _parse_skill_md(path: str) -> tuple:
    """解析 SKILL.md：返回 (frontmatter 字典, 正文)。支持 YAML 续行与中文字段。"""
    meta, body = {}, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(SKILL_MD_MAX_BYTES)
    except OSError:
        return meta, body
    lines = raw.splitlines()
    end = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        cur = None
        for ln in lines[1:end]:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
            if m and not ln[:1].isspace():
                cur = m.group(1)
                meta[cur] = m.group(2).strip()
            elif cur and ln[:1].isspace():
                meta[cur] = (meta.get(cur, "") + " " + ln.strip()).strip()
        for k, v in list(meta.items()):
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            meta[k] = re.sub(r"\s+", " ", v).strip()
    body = "\n".join(lines[end + 1:]) if end else raw
    return meta, body.strip()


def _skill_entry_from_dir(dirpath: str):
    md = os.path.join(dirpath, "SKILL.md")
    if not os.path.isfile(md):
        return None
    meta, body = _parse_skill_md(md)
    folder = os.path.basename(dirpath.rstrip("/"))
    name = (meta.get("name_cn") or meta.get("name") or folder).strip()
    desc = (meta.get("description_cn") or meta.get("description") or "").strip()
    desc = re.sub(r"\s+", " ", desc)[:140]
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", folder).strip("-").lower() or "skill"
    head = (f"【技能：{name}（来自 TeleAgent · 提示词型）】请按下面这份技能说明完成任务。"
            "说明里提到的脚本、命令行工具或依赖若本机不可用，请用你自身能力完成等效产出，"
            "不要声称执行了未实际执行的步骤。\n\n")
    return {
        "id": f"ta_{slug}", "name": name, "icon": "🧩", "desc": desc or "（无描述）",
        "instr": head + body[:SKILL_BODY_LIMIT],
        "out": "md", "source": "TeleAgent", "dir": dirpath,
        "chars": len(body), "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _load_store() -> dict:
    """读取技能库 v2 存储；旧 v1 自动迁移（skills → imported）"""
    try:
        with open(SKILLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and data.get("version") == 2:
        data.setdefault("imported", [])
        data.setdefault("custom", [])
        data.setdefault("overrides", {})
        return data
    imported = []
    if isinstance(data, dict):
        s = data.get("skills")
        if isinstance(s, list):
            imported = [x for x in s if isinstance(x, dict) and x.get("id")]
    return {"version": 2, "updated_at": (data or {}).get("updated_at", "") if isinstance(data, dict) else "",
            "imported": imported, "custom": [], "overrides": {}}


def _save_store(store: dict) -> None:
    """原子写入（tmp + os.replace），与索引状态文件同机制"""
    store["version"] = 2
    store["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    tmp = SKILLS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SKILLS_FILE)


def load_imported_skills() -> list:
    return _load_store()["imported"]


def refresh_imported_skills() -> dict:
    """扫描 TeleAgent 技能目录并同步「导入层」（只重写 imported，不影响我的技能与内置覆盖）"""
    old = {s["id"]: s for s in load_imported_skills()}
    scanned, dirs = {}, teleagent_skill_dirs()
    for d in dirs:
        try:
            subdirs = sorted(os.listdir(d))
        except OSError:
            continue
        for sub in subdirs:
            subdir = os.path.join(d, sub)
            if not os.path.isdir(subdir):
                continue
            entry = _skill_entry_from_dir(subdir)
            if not entry:
                continue
            if entry["id"] not in scanned:      # 同名技能以先扫描到的目录为准
                scanned[entry["id"]] = entry
    added = [i for i in scanned if i not in old]
    updated = [i for i in scanned if i in old and
               (old[i].get("name") != scanned[i].get("name") or
                old[i].get("instr") != scanned[i].get("instr") or
                old[i].get("desc") != scanned[i].get("desc"))]
    removed = [i for i in old if i not in scanned]
    for i in scanned:
        if i in old and i not in updated:
            scanned[i]["imported_at"] = old[i].get("imported_at") or scanned[i]["imported_at"]
    store = _load_store()
    store["imported"] = sorted(scanned.values(), key=lambda x: x["name"])
    _save_store(store)
    return {"ok": True, "dirs": dirs, "total": len(store["imported"]),
            "added": added, "updated": updated, "removed": removed,
            "custom_count": len(store["custom"])}


def builtin_skills() -> dict:
    """出厂内置 + 本地覆盖层合并（V1.8.0）"""
    overrides = _load_store()["overrides"]
    out = {}
    for k, v in WRITE_SKILLS.items():
        s = {**v, "kind": "builtin", "out": v.get("out", "md")}
        ov = overrides.get(k)
        if isinstance(ov, dict):
            for key in ("name", "icon", "desc", "out", "instr", "contract", "updated_at"):
                if key in ov:
                    s[key] = ov[key]
            s["overridden"] = True
        s["contract"] = s.get("contract") or ("embedded" if s["out"] in OUT_CONTRACTS else "none")
        out[k] = s
    return out


def custom_skills() -> list:
    return _load_store()["custom"]


def all_skills() -> dict:
    """全部技能（含导入层）：供技能管理页与写作接口解析（旧选型兼容）"""
    out = builtin_skills()
    for s in custom_skills():
        out[s["id"]] = {**s, "kind": "custom"}
    for s in load_imported_skills():
        out[s["id"]] = {**s, "kind": "imported"}
    return out


def writable_skills() -> dict:
    """V1.8.1：写作工作台可选技能 = 内置（含覆盖）+ 我的技能。

    TeleAgent 导入层不进入写作台菜单——导入技能需先「固化」为我的技能才能使用；
    写作接口仍按 all_skills 解析，历史调用选导入技能不受影响。"""
    out = builtin_skills()
    for s in custom_skills():
        out[s["id"]] = {**s, "kind": "custom"}
    return out


def _new_custom_id(existing_ids: set, base: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", str(base)).strip("-").lower() or "skill"
    sid = ("my_" + slug)[:60]
    if sid not in existing_ids:
        return sid
    i = 2
    while f"{sid}-{i}" in existing_ids:
        i += 1
    return f"{sid}-{i}"


def save_skill(base_id: str, data: dict) -> dict:
    """V1.8.0 保存技能：
    base_id 为空 → 新建「我的技能」；
    base_id 为内置 key → 保存为覆盖层（不动代码，可 reset 恢复出厂）；
    base_id 为「我的技能」id → 就地更新；
    base_id 为导入技能 id → 固化为「我的技能」副本（origin 溯源）。"""
    name = (data.get("name") or "").strip()
    out_kind = (data.get("out") or "md").strip()
    instr = (data.get("instr") or "").strip()
    if not name:
        return {"ok": False, "error": "技能名称不能为空"}
    if len(name) > 40:
        return {"ok": False, "error": "技能名称过长（不超过 40 字）"}
    if out_kind not in SKILL_OUT_KINDS:
        return {"ok": False, "error": "输出类型仅支持 Markdown / HTML / PPT / Excel"}
    if not instr:
        return {"ok": False, "error": "提示词不能为空"}
    if len(instr) > SKILL_INSTR_MAX:
        return {"ok": False, "error": f"提示词过长（不超过 {SKILL_INSTR_MAX} 字）"}
    icon = (data.get("icon") or "🧩").strip() or "🧩"
    desc = re.sub(r"\s+", " ", (data.get("desc") or "")).strip()[:140]
    contract = _detect_contract(out_kind, instr)
    store = _load_store()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if base_id in WRITE_SKILLS:                       # 内置 → 覆盖层
        store["overrides"][base_id] = {"name": name, "icon": icon, "desc": desc,
                                       "out": out_kind, "instr": instr,
                                       "contract": contract, "updated_at": now}
        _save_store(store)
        return {"ok": True, "id": base_id, "mode": "override"}

    custom = store["custom"]
    old = next((s for s in custom if s.get("id") == base_id), None)
    origin = None
    if base_id:
        imp = next((s for s in store["imported"] if s.get("id") == base_id), None)
        origin = imp["id"] if imp else (old or {}).get("origin")
        if not old and not imp and base_id not in ("", "new"):
            return {"ok": False, "error": f"未找到基准技能 {base_id}"}
    if old:                                            # 就地更新
        old.update({"name": name, "icon": icon, "desc": desc, "out": out_kind,
                    "instr": instr, "contract": contract, "chars": len(instr),
                    "updated_at": now})
        sid, mode = old["id"], "custom_update"
    else:                                              # 新建（含从导入固化）
        sid = _new_custom_id({s["id"] for s in custom} | set(WRITE_SKILLS),
                             base_id if base_id and base_id != "new" else name)
        custom.append({"id": sid, "name": name, "icon": icon, "desc": desc,
                       "out": out_kind, "instr": instr, "contract": contract,
                       "origin": origin, "created_at": now, "updated_at": now,
                       "chars": len(instr)})
        mode = "promote" if origin else "create"
    store["custom"] = sorted(custom, key=lambda x: x.get("name", ""))
    _save_store(store)
    return {"ok": True, "id": sid, "mode": mode}


def reset_builtin(skill_id: str) -> dict:
    """内置技能恢复出厂：移除覆盖层"""
    if skill_id not in WRITE_SKILLS:
        return {"ok": False, "error": "仅内置技能支持恢复出厂"}
    store = _load_store()
    if skill_id not in store["overrides"]:
        return {"ok": False, "error": "该内置技能未被修改过"}
    store["overrides"].pop(skill_id)
    _save_store(store)
    return {"ok": True}


def promote_imported(skill_id: str) -> dict:
    """导入技能一键固化为「我的技能」：提示词全文落库，不再依赖 TeleAgent，可继续编辑"""
    store = _load_store()
    imp = next((s for s in store["imported"] if s.get("id") == skill_id), None)
    if not imp:
        return {"ok": False, "error": "未找到该导入技能"}
    instr = imp.get("instr") or ""
    cut = instr.find("】")                             # 去掉「（来自 TeleAgent · 提示词型）」头
    body = instr[cut + 1:].strip() if cut > 0 else instr
    new_head = (f"【技能：{imp['name']}】请按下面这份技能说明完成任务。"
                "说明里提到的脚本、命令行工具或依赖若本机不可用，请用你自身能力完成等效产出，"
                "不要声称执行了未实际执行的步骤。\n\n")
    return save_skill(skill_id, {"name": imp.get("name", "")[:40],
                                 "icon": imp.get("icon") or "🧩",
                                 "desc": imp.get("desc") or "",
                                 "out": imp.get("out") or "md",
                                 "instr": new_head + body})


def delete_skill(skill_id: str) -> dict:
    """删除：我的技能 / 导入层；内置不可删（改用恢复出厂）"""
    if skill_id in WRITE_SKILLS:
        store = _load_store()
        if skill_id in store["overrides"]:
            return {"ok": False, "error": "内置技能不可删除；已修改的请用「恢复出厂」"}
        return {"ok": False, "error": "内置技能不可删除"}
    store = _load_store()
    custom = [s for s in store["custom"] if s.get("id") != skill_id]
    if len(custom) != len(store["custom"]):
        store["custom"] = custom
        _save_store(store)
        return {"ok": True, "mode": "custom"}
    imported = [s for s in store["imported"] if s.get("id") != skill_id]
    if len(imported) != len(store["imported"]):
        store["imported"] = imported
        _save_store(store)
        return {"ok": True, "mode": "imported"}
    return {"ok": False, "error": "未找到该技能"}


def resolve_skill(skill_key: str) -> dict:
    return all_skills().get(skill_key) or WRITE_SKILLS["doc"]


# ============================================================
# V1.7.16：样例格式 ↔ 技能输出格式一致性约束
#   样例文档必须与技能最终产出同属一个格式族，否则仿写无意义：
#   文档族（Markdown/可导出 Word）← doc/docx/pdf/txt/md/markdown
#   HTML 族 ← html/htm；PPT 族 ← pptx/ppt/pdf；Excel 族 ← xlsx/xls/csv
#   V1.7.17：PPT 族补入旧版 .ppt（本地二进制解析）与 .pdf（演示稿常见导出件）
# ============================================================
SAMPLE_FORMATS = {
    "md":   ["md", "markdown", "doc", "docx", "pdf", "txt"],
    "html": ["html", "htm"],
    "pptx": ["pptx", "ppt", "pdf"],
    "xlsx": ["xlsx", "xls", "csv"],
}
SAMPLE_FORMAT_LABELS = {
    "md": "Word / Markdown / PDF / TXT",
    "html": "HTML",
    "pptx": "PPT / PPTX / PDF",
    "xlsx": "Excel / CSV",
}


def sample_exts_for(out_kind: str) -> list:
    """技能输出类型 → 允许的样例扩展名（不含点）"""
    return SAMPLE_FORMATS.get(out_kind or "md", SAMPLE_FORMATS["md"])


def sample_ext_ok(path_or_name: str, out_kind: str) -> bool:
    """样例文件扩展名是否与技能输出格式兼容"""
    ext = os.path.splitext(str(path_or_name or ""))[1].lower().lstrip(".")
    return ext in sample_exts_for(out_kind)


def _template_skeleton(text: str) -> str:
    """v5.2.13：提取模版文档的章节骨架（标题行），供仿写时聚焦结构参考"""
    heads = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or len(s) > 60:
            continue
        if (s.startswith("#") or re.match(r"^[一二三四五六七八九十]+[、.．]", s)
                or re.match(r"^[（(][一二三四五六七八九十\d]+[)）]", s)
                or re.match(r"^\d+[、.．]", s) or s.endswith("：") or s.endswith(":")):
            heads.append(s.lstrip("# ").strip())
    return "；".join(dict.fromkeys(heads))[:1200]   # 去重保序


TEMPLATE_MAX_CHARS = 4000   # 模版正文节选注入上限（保护提示词长度，结构参考足够）


def build_messages(params: dict, materials: list, outline: str = "") -> list:
    """构建 OpenAI 格式消息：专家人设 + 技能指令 + 结构要求 + 主题要点 + 素材/附件（+ 自动提纲）"""
    tpl = DOC_TEMPLATES.get(params.get("doc_type"), DOC_TEMPLATES["custom"])
    skill = resolve_skill(params.get("skill"))
    expert = WRITE_EXPERTS.get(params.get("expert"), WRITE_EXPERTS["general"])
    length = LENGTH_DESC.get(params.get("length"), LENGTH_DESC["medium"])
    style = STYLE_DESC.get(params.get("style"), STYLE_DESC["formal"])
    # V1.8.0：自定义/固化技能选了 HTML/PPT/Excel 输出时，由引擎追加标准输出契约
    #（内置技能与自带完整格式要求的提示词不加，避免重复）
    instr = skill["instr"]
    if skill.get("contract") == "auto" and skill.get("out") in OUT_CONTRACTS:
        instr = instr + "\n\n【输出格式要求】\n" + OUT_CONTRACTS[skill["out"]]
    system = expert["persona"] + "\n\n" + instr

    mat_block = ""
    if materials:
        parts = []
        for i, m in enumerate(materials, 1):
            # V1.8.5：素材行带「更新」日期，模型据此判断新旧
            upd = (m.get("updated") or "").strip()
            tag = f"[素材{i}｜来源：{m['source']}" + (f"｜更新：{upd}" if upd else "") + "]"
            parts.append(f"{tag}\n{m['text']}")
        mat_block = ("\n\n【知识库素材】（来自超脑知识库检索的事实参考，请甄别时效性与适用性；"
                     "同一知识点存在多个版本或多处表述时，优先采用「更新」日期较新的素材；"
                     "数据冲突时以较新者为准，并在文中自然注明「截至YYYY年MM月」；"
                     "未覆盖的内容基于常识合理撰写，不得虚构素材中不存在的数据）\n\n"
                     + "\n\n".join(parts))
    atts = params.get("attachments") or []
    if atts:
        parts = []
        for i, a in enumerate(atts, 1):
            parts.append(f"[附件{i}｜{a.get('name', '附件')}]\n{(a.get('text') or '')[:20000]}")
        mat_block += ("\n\n【用户附件素材】（用户随任务附带的本机文档节选，可信度高于知识库素材，优先采用）\n\n"
                      + "\n\n".join(parts))

    template_text = (params.get("template_text") or "").strip()
    if template_text:
        # v5.2.13 仿写模式：结构要求由模版文档主导，内置大纲仅作兜底基调参考
        skeleton = _template_skeleton(template_text)
        struct_block = f"""【结构要求】（仿写模式）
本篇为文档仿写任务：请严格参照下方《模版文档》的格式、章节架构与行文组织方式，撰写一篇同类型的新文档。要求：
1. 模仿模版的标题层级与编号方式（模版用「一、（一）1.」编号则沿用编号，用「# / ##」标题则沿用标题），章节划分逻辑保持一致；
2. 只借鉴模版的格式与架构，禁止照搬模版中的具体事实、数据与人物——新文档的全部事实内容必须来自【主题】【要点与要求】与【知识库素材】；
3. 行文基调：{tpl['tone']}；整体风格：{style}；{length}。
模版章节骨架：{skeleton or "（模版无明显标题结构，请通读正文节选后自行模仿其组织方式）"}

【模版文档正文节选】
{template_text[:TEMPLATE_MAX_CHARS]}"""
    else:
        struct_block = f"""【结构要求】
参考结构：{tpl['outline']}
行文基调：{tpl['tone']}；整体风格：{style}；{length}。
（如与所选技能的输出格式要求冲突，以技能要求为准）"""

    outline_block = ""
    if (outline or "").strip():
        outline_block = ("\n\n【写作提纲】（自动生成的提纲，请严格按其章节结构展开成稿，"
                         "可细化但不要偏离框架）\n" + outline.strip()[:3000])

    # V1.8.4 修订模式：基于上一稿全文 + 用户修改意见重写完整新稿（修订轮 materials 为空，不再检索）
    prev = (params.get("previous_text") or "").strip()
    fb = (params.get("feedback") or "").strip()
    if prev and fb:
        user = f"""这是一份文档修订任务（技能：{skill['name']}；专家：{expert['name']}）。
此前已生成初稿，用户对初稿提出了修改意见，请基于初稿重写并输出完整的新稿。

{struct_block}{outline_block}

【原任务】
主题：{params.get('topic', '')}
要点与要求：{params.get('points') or '（无）'}{mat_block}

【用户修改意见】（须逐条落实，新增要求同步生效）
{fb[:4000]}

【初稿全文】
{prev[:30000]}

【输出要求】
1. 输出修订后的完整全文，不要只输出改动部分，不要用代码围栏包裹文稿；
2. 保留初稿的整体结构与正确内容，仅针对修改意见调整；意见含新增要求时，在合适位置补充新内容；
3. 输出格式（Markdown / HTML / JSON 等）与标题层级编号与初稿保持一致，不得变换格式；
4. 不要输出任何与交付物无关的解释、寒暄或过程说明。"""
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    user = f"""请为我完成以下写作任务（技能：{skill['name']}；专家：{expert['name']}）。

{struct_block}{outline_block}

【主题】
{params.get('topic', '')}

【要点与要求】
{params.get('points') or '（无特别要求，请结合主题合理展开）'}{mat_block}

【输出要求】
1. 严格遵循本任务技能要求的输出格式（Markdown / HTML / JSON 等），不得偏差；
2. 素材中的事实与数据自然融入行文，不单独标注素材编号（深度研究技能按其自身规则标注来源）；
3. 不要输出任何与交付物无关的解释、寒暄或过程说明。"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def build_outline_messages(params: dict, materials: list) -> list:
    """V1.5.6 全自动执行第一步：仅生成详细写作提纲"""
    base = build_messages(params, materials)
    system, user = base[0]["content"], base[1]["content"]
    user += ("\n\n【本步任务】先不要写正文：请只输出本篇文档的详细提纲——一级/二级标题与每节要点"
             "（每条一句话），用 Markdown 列表呈现。")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _collect_stream(messages: list, cfg: dict, max_chars: int = 4000) -> str:
    """非流式收集（用于自动提纲等辅助步）：达到字符上限即停止，控制成本"""
    parts, size = [], 0
    for delta in llm_chat_stream(messages, cfg):
        parts.append(delta)
        size += len(delta)
        if size >= max_chars:
            break
    return "".join(parts)


# ============================================================
# OpenAI 兼容流式调用
# ============================================================
def llm_chat_stream(messages: list, cfg: dict):
    """调用 OpenAI 兼容 /chat/completions 流式接口，逐段产出文本"""
    base = normalize_base_url(cfg["base_url"])
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg.get('api_key', '')}"}
    payload = {
        "model": cfg["model"], "messages": messages, "stream": True,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 4096)),
    }
    try:
        r = requests.post(url, json=payload, headers=headers, stream=True,
                          timeout=(10, int(cfg.get("timeout", 300))))
    except requests.exceptions.Timeout:
        raise RuntimeError("连接大模型接口超时，请检查接口地址")
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError("无法连接大模型接口：" + re.sub(r"\s+", " ", str(e))[:120])
    if r.status_code in (401, 403):
        raise RuntimeError(f"鉴权失败（HTTP {r.status_code}）：请检查 API Key")
    if r.status_code == 404:
        raise RuntimeError("接口返回 404：请检查接口地址（通常需含 /v1）与模型名")
    if r.status_code != 200:
        raise RuntimeError(f"接口返回 HTTP {r.status_code}：" + r.text[:150])
    r.encoding = "utf-8"   # SSE 流按 UTF-8 解码（响应无 charset 时 requests 默认 latin-1，会导致中文乱码）
    try:
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = (choices[0].get("delta") or {}).get("content") or ""
            if delta:
                yield delta
    finally:
        r.close()


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _extract_materials(search_result: dict, top_n: int) -> list:
    """从引擎检索结果提取写作素材：优先用组装上下文，截断保护提示词长度"""
    mats = []
    for c in (search_result.get("results") or [])[:top_n]:
        text = (c.get("context") or c.get("text") or "").strip()
        if text:
            # V1.8.5：透出文档更新日期，供模型判断新旧与冲突取舍
            try:
                mt = float(c.get("file_mtime") or 0)
            except (TypeError, ValueError):
                mt = 0.0
            updated = (datetime.fromtimestamp(mt).strftime("%Y-%m-%d") if mt > 0
                       else str(c.get("build_date") or ""))
            mats.append({"source": c.get("source", ""), "text": text[:1500],
                         "filename": c.get("filename", ""), "score": c.get("rerank_score", 0),
                         "updated": updated})
    return mats


def stream_write(params: dict, search_fn):
    """
    写作主流程（SSE 事件流生成器）：
      status → sources → delta* → done（V1.5.6：done 可带生成文件信息 file）
    V1.5.6：支持技能/专家/工作流/模型档位/附件；全自动执行＝先提纲再成稿；
    深度研究自动多轮检索；html/pptx/xlsx 技能在完成后由本地管线落盘。
    """
    t0 = time.time()
    cfg = resolve_model(params.get("model_id") or "")   # V1.5.8：按所选模型解析接口/Key/参数
    if not llm_configured(cfg):
        yield _sse({"type": "error",
                   "data": "尚未配置大模型接口：请到「模型配置」页添加模型并填写接口地址、模型名与 API Key"})
        return

    skills = all_skills()
    skill_key = params.get("skill") if params.get("skill") in skills else "doc"
    skill = skills[skill_key]
    expert = WRITE_EXPERTS.get(params.get("expert"), WRITE_EXPERTS["general"])
    model_show = cfg.get("model", "")
    if model_show.startswith("NewApi/"):             # 路由标识不外露
        model_show = "已配置模型"
    out_kind = skill.get("out", "md")                # V1.6.0：输出成果按技能类型
    out_label = {"md": "Markdown（可导出 Word）", "html": "HTML 文件",
                 "pptx": "PPTX 文件", "xlsx": "Excel 文件"}.get(out_kind, out_kind)
    # V1.8.2：文档写作 + Word 模版仿写 → 产出直接为 Word 文件，不再停在 Markdown
    auto_docx = (skill_key == "doc" and out_kind == "md"
                 and (params.get("template_text") or "").strip()
                 and os.path.splitext(params.get("template_name") or "")[1].lower() in (".docx", ".doc"))
    if auto_docx:
        out_label = "Word 文档（按 Word 模版仿写，自动导出）"
    # V1.8.4 修订模式：带 previous_text + feedback 时不检索知识库、不生成提纲，直接基于上一稿重写
    is_revise = bool((params.get("previous_text") or "").strip()
                     and (params.get("feedback") or "").strip())
    # V1.7.16：样例与技能输出格式必须一致，否则无法按样例仿写（前端已拦截，此处为接口层兜底）
    tpl_local = (params.get("template_local_path") or "").strip()
    if tpl_local and not sample_ext_ok(tpl_local, out_kind):
        _bad_ext = os.path.splitext(tpl_local)[1].lower().lstrip(".") or "未知"
        yield _sse({"type": "error",
                    "data": f"样例《{os.path.basename(tpl_local)}》是 {_bad_ext.upper()} 格式，"
                            f"与当前技能「{skill['name']}」的输出格式（{out_label}）不一致，无法按样例仿写。"
                            f"请改选 {SAMPLE_FORMAT_LABELS.get(out_kind, out_kind)} 格式的样例，或更换技能。"})
        return
    if out_kind in ("pptx", "xlsx", "html"):
        # 结构化产出的 JSON 往往超过默认 4096 tokens，截断会导致解析失败（V1.7.0 修复）
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 4096), 8192)
    yield _sse({"type": "status",
                "data": f"技能：{skill['name']} · 专家：{expert['name']} · 模型：{cfg.get('model_name') or model_show}"
                        f" · 输出：{out_label}" + (" · 修订模式" if is_revise else "")})

    materials, source_list = [], []
    tpl_text = (params.get("template_text") or "").strip()
    if tpl_text:
        tpl_name = params.get("template_name") or "模版"
        yield _sse({"type": "status",
                    "data": f"已载入模版《{tpl_name}》（{len(tpl_text)} 字），将按其格式与架构仿写..."})
    elif params.get("template_load_failed"):
        yield _sse({"type": "status",
                    "data": f"⚠️ {params.get('template_fail_reason') or '模版加载失败'}，本次按普通模式写作..."})
    if is_revise:
        # V1.8.4：上一稿已融合过素材事实，修订轮不再检索，避免状态抖动与耗时
        yield _sse({"type": "status", "data": "修订模式：基于上一稿与修改意见重写（本轮不检索知识库）..."})
    elif params.get("use_kb"):
        yield _sse({"type": "status", "data": "正在检索知识库素材..."})
        query = (params.get("kb_query") or params.get("topic") or "").strip()
        top_k = max(2, min(int(params.get("top_k") or 8), 12))
        queries = [query, query + " 数据 案例 现状"] if skill_key == "research" and query else [query]
        seen = set()
        try:
            for q in queries:
                result = search_fn(q, top_k)
                for m in _extract_materials(result, top_k):
                    key = (m["source"], (m["text"] or "")[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    materials.append(m)
            source_list = [{"source": m["source"], "name": m["filename"] or m["source"],
                            "score": m["score"]} for m in materials]
            if materials:
                yield _sse({"type": "status", "data": f"已获取 {len(materials)} 条知识库素材，正在生成..."})
                yield _sse({"type": "sources", "data": source_list})
            else:
                yield _sse({"type": "status", "data": "知识库未检索到相关素材，将直接根据主题撰写..."})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "status", "data": f"知识库检索暂不可用（{e}），将直接根据主题撰写..."})
    else:
        yield _sse({"type": "status", "data": "正在调用大模型生成..."})

    # V1.5.6 全自动执行 / 提纲先行工作流：先生成提纲（结构化输出类技能跳过）
    outline = ""
    if (not is_revise) and (params.get("auto_flow") or params.get("workflow") == "outline") and skill_key in ("doc", "research"):
        yield _sse({"type": "status", "data": "全自动执行：正在生成写作提纲..."})
        try:
            outline = _collect_stream(build_outline_messages(params, materials), cfg, max_chars=3000).strip()
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "status", "data": f"提纲生成失败（{e}），直接成稿..."})
        if outline:
            yield _sse({"type": "status", "data": f"提纲已生成（{len(outline)} 字），开始成稿..."})

    messages = build_messages(params, materials, outline)
    acc, chunks = [], 0
    try:
        for delta in llm_chat_stream(messages, cfg):
            acc.append(delta)
            chunks += 1
            yield _sse({"type": "delta", "data": delta})
    except RuntimeError as e:
        yield _sse({"type": "error", "data": str(e)})
        return
    except Exception as e:  # noqa: BLE001
        yield _sse({"type": "error", "data": "生成失败：" + str(e)[:150]})
        return
    text = "".join(acc)
    if not text.strip():
        yield _sse({"type": "error", "data": "大模型未返回任何内容，请检查模型名是否正确"})
        return

    # V1.6.0 产出落盘：按技能声明的输出类型（html/pptx/xlsx 落盘；md 由界面提供 Word 导出）
    file_info = None
    if out_kind in ("html", "pptx", "xlsx"):
        try:
            file_info = save_generated_output(text, params, cfg, out_kind=out_kind)
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "status", "data": f"⚠️ 文件生成失败（{e}），已保留原始输出"})
    elif auto_docx:
        # V1.8.2：doc 技能 + Word 模版 → 生成后直接导出 Word 文件（交付物不再是 Markdown）
        # V1.8.3：按模版版式克隆导出（页面/标题/章节/正文/落款与模版一致）；
        #         文件名取生成正文的一级标题，继承模版"关于开展XXX的通知"命名风格
        try:
            tpl_path = (params.get("template_local_path") or "").strip()
            if not tpl_path and params.get("template_file"):
                from rag_core import DOCS_DIR
                tpl_path = os.path.join(DOCS_DIR, params["template_file"])
            fallback_title = (params.get("topic") or "").strip()[:40] or "仿写文档"
            if tpl_path and os.path.splitext(tpl_path)[1].lower() == ".docx" and os.path.isfile(tpl_path):
                r = export_docx_like_template(tpl_path, text, fallback_title)
            else:
                r = export_docx(fallback_title, text)   # .doc 老格式或模版路径失效 → 通用导出
            if r.get("ok"):
                file_info = {"kind": "docx", "path": r["path"], "name": r["filename"],
                             "dir": os.path.dirname(r["path"])}
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "status", "data": f"⚠️ Word 自动导出失败（{e}），已保留 Markdown 输出"})

    data = {"chars": len(text), "elapsed": round(time.time() - t0, 1),
            "sources": source_list, "skill": skill_key, "out": out_kind}
    if is_revise:
        data["revise"] = True      # V1.8.4：前端据此显示「已修订」并清空意见框
    if file_info:
        data["file"] = file_info
    yield _sse({"type": "done", "data": data})


# ============================================================
# V1.5.6：产出落盘（HTML / PPTX / XLSX）
# ============================================================
def _output_dir_for(params: dict) -> str:
    if (params.get("output_dir") or "writer_docs") == "workspace":
        d = os.path.dirname(KB_ROOT)
    else:
        d = WRITER_DOCS_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def _extract_html_doc(text: str) -> str:
    """V1.8.0：从模型输出提取真正的 HTML 文档体。

    模型（尤其 deepseek-flash）常在代码块前加一句说明文字、后面跟 ```html 围栏，
    单纯去围栏会残留开场白/尾注。策略：从首个 <!DOCTYPE / <html 标记截取开头；
    末尾定位 </html>（文档真正结束处）截断，否则回退常规去围栏。"""
    t = (text or "").strip()
    pos = -1
    for tag in ("<!DOCTYPE", "<!doctype", "<html", "<HTML"):
        p = t.find(tag)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos > 0:
        t = t[pos:]
    m = re.search(r"</html\s*>", t, re.I)     # 丢弃 </html> 之后的围栏与尾注
    if m:
        return t[:m.end()].strip()
    return _strip_fence(t)


def _repair_json_text(t: str) -> str:
    """V1.7.0：修复模型常见的 JSON 瑕疵——字符串内未转义的英文引号、尾随逗号、字符串内裸换行"""
    out, in_str, esc = [], False, False
    for i, ch in enumerate(t):
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            j = i + 1
            while j < len(t) and t[j] in " \t\r\n":
                j += 1
            nxt = t[j] if j < len(t) else ""
            if nxt in ",:}]" or nxt == "":
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')     # 字符串内部的引号 → 转义
            continue
        if ch == "\n" and in_str:
            out.append("\\n")
            continue
        out.append(ch)
    fixed = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", fixed)     # 去掉尾随逗号


def repair_json_via_llm(raw: str, cfg: dict) -> dict:
    """V1.7.3：JSON 语法坏掉时，让模型自己修——只允许输出修正后的合法 JSON。
    修复常见的：中文里的未转义英文引号、数字千分位逗号、尾随逗号、被截断。"""
    head = (raw or "")[:16000]
    messages = [
        {"role": "system", "content": "你是 JSON 修复器。你只能输出修正后的合法 JSON 本体，"
                                       "不要解释、不要 Markdown 代码块、不要任何其他文字。"},
        {"role": "user", "content": "下面这份 JSON 有语法错误（常见原因：字符串内英文引号未转义、"
                                    "数字带千分位逗号如 1,234、尾随逗号、内容被截断）。"
                                    "请修复语法并完整保留原有内容；若被截断，请合理补全为完整 JSON。"
                                    "只输出修正后的 JSON。\n\n" + head},
    ]
    fixed_cfg = dict(cfg)
    fixed_cfg["temperature"] = 0.0
    text = _collect_stream(messages, fixed_cfg, max_chars=30000)
    if not text.strip():
        raise ValueError("修复请求未返回内容")
    return _extract_json_block(text)


def _extract_json_block(text: str) -> dict:
    t = _strip_fence(text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("模型输出未包含有效 JSON")
    frag = t[i:j + 1]
    try:
        return json.loads(frag)
    except json.JSONDecodeError as e:
        try:
            return json.loads(_repair_json_text(frag))
        except json.JSONDecodeError:
            logger.warning("JSON 修复失败：%s｜头部=%r｜尾部=%r", e, frag[:100], frag[-100:])
            raise





def _output_title(params: dict, text: str, data: dict = None) -> str:
    if isinstance(data, dict) and data.get("title"):
        return str(data["title"]).strip()[:60]
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        return m.group(1).strip()[:60]
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if not m:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
    if m:
        return (re.sub(r"<[^>]+>", "", m.group(1)).strip()[:60]) or "写作成果"
    return (params.get("topic") or "写作成果").strip()[:60] or "写作成果"


def salvage_deck_spec(text: str, title: str = "", expert: str = "general") -> dict:
    """V1.7.0：模型输出的 JSON 被截断时，按字段尽量抢救出可用页面，避免整份退化成一段乱码"""
    from rag_pptx import deck_from_markdown

    t = _strip_fence(text or "")
    titles = re.findall(r'"title"\s*:\s*"([^"]{2,60})"', t)
    bodies = re.findall(r'"bullets"\s*:\s*\[(.*?)\]', t, re.S)
    slides = []
    for i, bt in enumerate(bodies):
        items = [re.sub(r'\s+', " ", x).strip() for x in re.findall(r'"([^"]{2,120})"', bt)]
        if items:
            slides.append({"type": "bullets",
                           "title": titles[i + 1] if i + 1 < len(titles) else (titles[i] if i < len(titles) else f"要点 {i + 1}"),
                           "bullets": items[:6], "subtitle": "", "kicker": "", "notes": ""})
    if len(slides) >= 2:
        return {"title": title or (titles[0] if titles else "演示文稿"), "subtitle": "",
                "theme": "business", "slides": slides}
    return deck_from_markdown(t, title=title, expert=expert)


def _build_pptx(data: dict, path: str, params: dict = None):
    """V1.7.0：交给设计系统版式引擎（rag_pptx），失败时回落 Markdown 转要点页"""
    from rag_pptx import build_deck

    params = params or {}
    expert = params.get("expert") or "general"
    info = build_deck(data, path, expert=expert)
    logger.info("PPTX 已生成：%s（%s 页，主题 %s）", path, info.get("slides"), info.get("theme"))
    return info


def _build_xlsx(data: dict, path: str, params: dict = None):
    """V1.7.2：交给 Excel 设计引擎（rag_xlsx），标题带/样式/公式合计/数据条/图表一次成型"""
    from rag_xlsx import build_workbook

    params = params or {}
    info = build_workbook(data, path, expert=params.get("expert") or "general")
    logger.info("XLSX 已生成：%s（%s 张表，主题 %s）", path, info.get("sheets"), info.get("theme"))
    return info


def save_generated_output(text: str, params: dict, cfg: dict = None, out_kind: str = None):
    """技能产出落盘：html → 单文件 HTML；pptx/xlsx → 本地生成可编辑文件。
    返回 {kind, path, name, dir} 或 None（md 类技能不落盘，走界面导出流程）。
    V1.8.0：落盘由 out_kind（技能声明的输出类型）决定，自定义/固化技能同样生效；
    旧调用未传 out_kind 时兼容内置技能（key 与输出类型一致的场票）。"""
    kind = out_kind or params.get("out_kind") or ""
    if kind not in ("html", "pptx", "xlsx") and params.get("skill") in ("html", "pptx", "xlsx"):
        kind = params["skill"]                        # 旧调用兼容
    skill = kind
    if not kind:
        return None
    out_dir = _output_dir_for(params)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if skill == "html":
        html = _extract_html_doc(text)
        title = _output_title(params, html)
        fname = f"{_safe_name(title, 40)}_{stamp}.html"
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return {"kind": "html", "path": path, "name": fname, "dir": out_dir}
    if skill == "pptx":
        from rag_pptx import build_deck, deck_from_markdown

        try:
            data = _extract_json_block(text)
        except (ValueError, json.JSONDecodeError) as jerr:
            logger.warning("PPT JSON 解析失败（%s），尝试让模型自动修复", jerr)
            try:
                data = repair_json_via_llm(text, cfg)
                logger.info("PPT JSON 自动修复成功")
            except Exception as rerr:  # noqa: BLE001
                logger.warning("PPT JSON 修复未成功（%s），改用结构化兜底", rerr)
                data = salvage_deck_spec(text, title=(params.get("topic") or "")[:60],
                                         expert=params.get("expert") or "general")
        title = _output_title(params, text, data)
        fname = f"{_safe_name(title, 40)}_{stamp}.pptx"
        path = os.path.join(out_dir, fname)
        info = build_deck(data, path, expert=params.get("expert") or "general")
        return {"kind": "pptx", "path": path, "name": fname, "dir": out_dir,
                "slides": info.get("slides", 0), "theme": info.get("theme"),
                "layouts": info.get("layouts") or []}
    if skill == "xlsx":
        from rag_xlsx import build_workbook, workbook_from_text

        try:
            data = _extract_json_block(text)
        except (ValueError, json.JSONDecodeError) as jerr:
            logger.warning("XLSX JSON 解析失败（%s），尝试让模型自动修复", jerr)
            try:
                data = repair_json_via_llm(text, cfg)
                logger.info("XLSX JSON 自动修复成功")
            except Exception as rerr:  # noqa: BLE001
                logger.warning("XLSX JSON 修复未成功（%s），改用文本表格兜底", rerr)
                data = workbook_from_text(text, title=(params.get("topic") or "")[:60],
                                          expert=params.get("expert") or "general")
        title = _output_title(params, text, data)
        fname = f"{_safe_name(title, 40)}_{stamp}.xlsx"
        path = os.path.join(out_dir, fname)
        info = build_workbook(data, path, expert=params.get("expert") or "general")
        return {"kind": "xlsx", "path": path, "name": fname, "dir": out_dir,
                "sheets": info.get("sheets", 0), "rows": info.get("rows", 0)}

# ============================================================
# 写作成果（保存/列表/读取/删除）
# ============================================================
def _safe_name(name: str, maxlen: int = 50) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n#]+", "_", (name or "").strip())[:maxlen]
    return name or "未命名文档"


def _load_index() -> dict:
    try:
        with open(WRITER_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(idx: dict):
    os.makedirs(WRITER_DOCS_DIR, exist_ok=True)
    with open(WRITER_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)


def save_doc(title: str, doc_type: str, content: str, sources: list = None) -> dict:
    os.makedirs(WRITER_DOCS_DIR, exist_ok=True)
    doc_id = datetime.now().strftime("%Y%m%d%H%M%S") + f"{os.getpid() % 1000:03d}"
    safe_title = _safe_name(title)
    # 正文已自带首行标题时不重复拼接，避免「# 标题」出现两次
    if (content or "").lstrip().startswith("#"):
        md = content + "\n"
    else:
        md = f"# {title}\n\n{content}\n"
    with open(os.path.join(WRITER_DOCS_DIR, f"{doc_id}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    idx = _load_index()
    idx[doc_id] = {
        "id": doc_id, "title": title or safe_title, "doc_type": doc_type,
        "doc_type_name": DOC_TEMPLATES.get(doc_type, {}).get("name", "自定义"),
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "chars": len(content), "sources": sources or [],
    }
    _save_index(idx)
    return {"ok": True, "id": doc_id, **idx[doc_id]}


def list_docs() -> dict:
    """V1.7.4：统一文档清单——index.json 中的 Markdown 文档 + writer_docs 里的技能产出文件（pptx/xlsx/html）。
    每条带 fmt（md/pptx/xlsx/html）与行内操作所需的全部字段。"""
    idx = _load_index()
    docs = []
    for d in idx.values():
        fpath = os.path.join(WRITER_DOCS_DIR, f"{d['id']}.md")
        if not os.path.isfile(fpath):
            continue
        docs.append({"id": d["id"], "title": d.get("title") or d["id"], "fmt": "md",
                     "created": d.get("created") or "", "chars": d.get("chars") or 0,
                     "size_kb": round(os.path.getsize(fpath) / 1024, 1),
                     "path": fpath, "sources": d.get("sources") or []})
    # 扫描技能产出文件（不进 index.json，按文件名识别）
    for fname in os.listdir(WRITER_DOCS_DIR):
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext not in ("pptx", "xlsx", "html", "docx"):
            continue
        fpath = os.path.join(WRITER_DOCS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
        title = re.sub(r"_\d{8}(_\d{2,6})?$|_\d{14}$", "", os.path.splitext(fname)[0]) or fname
        docs.append({"id": fname, "title": title, "fmt": ext,
                     "created": mtime.strftime("%Y-%m-%d %H:%M"),
                     "chars": 0, "size_kb": round(os.path.getsize(fpath) / 1024, 1),
                     "path": fpath, "sources": []})
    docs.sort(key=lambda d: d["created"], reverse=True)
    # 兼容旧字段
    for d in docs:
        d["doc_type_name"] = {"md": "Markdown 文档", "pptx": "PPT 演示", "xlsx": "Excel 表格",
                              "html": "HTML 文档", "docx": "Word 文档"}.get(d["fmt"], d["fmt"])
    return {"docs": docs, "total": len(docs)}


def get_doc(doc_id: str) -> dict:
    doc_id = os.path.basename(doc_id or "")
    # V1.7.4：技能产出文件（文件名即 id）——返回元信息，正文不读入（二进制/HTML 由本机应用打开）
    if os.path.splitext(doc_id)[1].lower().lstrip(".") in ("pptx", "xlsx", "html", "docx"):
        fpath = os.path.join(WRITER_DOCS_DIR, doc_id)
        if not os.path.isfile(fpath):
            return {"error": f"文件不存在：{doc_id}"}
        fmt = os.path.splitext(doc_id)[1].lower().lstrip(".")
        return {"doc": {"id": doc_id, "title": re.sub(r"_\d{8}(_\d{2,6})?$|_\d{14}$", "", os.path.splitext(doc_id)[0]),
                        "fmt": fmt, "path": fpath,
                        "doc_type_name": {"pptx": "PPT 演示", "xlsx": "Excel 表格",
                                          "html": "HTML 文档", "docx": "Word 文档"}.get(fmt, fmt)},
                "content": ""}
    idx = _load_index()
    if doc_id not in idx:
        return {"error": f"文档不存在：{doc_id}"}
    try:
        with open(os.path.join(WRITER_DOCS_DIR, f"{doc_id}.md"), "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {"error": "文档文件读取失败"}
    d = idx[doc_id]
    d["fmt"] = "md"
    d["path"] = os.path.join(WRITER_DOCS_DIR, f"{doc_id}.md")
    return {"doc": d, "content": content}


def delete_doc(doc_id: str) -> dict:
    doc_id = os.path.basename(doc_id or "")
    ext = os.path.splitext(doc_id)[1].lower().lstrip(".")
    # V1.7.4：技能产出文件直接按文件名删除（限 writer_docs 内、白名单后缀）
    if ext in ("pptx", "xlsx", "html", "docx"):
        fpath = os.path.join(WRITER_DOCS_DIR, doc_id)
        if not os.path.isfile(fpath):
            return {"error": f"文件不存在：{doc_id}"}
        os.remove(fpath)
        return {"ok": True}
    idx = _load_index()
    if doc_id not in idx:
        return {"error": f"文档不存在：{doc_id}"}
    try:
        os.remove(os.path.join(WRITER_DOCS_DIR, f"{doc_id}.md"))
    except OSError:
        pass
    idx.pop(doc_id, None)
    _save_index(idx)
    return {"ok": True}


# ============ V1.8.3：模版版式克隆导出（doc 技能 + Word 模版仿写专用） ============

def _tpl_section_fmt(sec):
    """采样模版节属性：页面尺寸与页边距（长度对象直接复用）"""
    return {"pw": sec.page_width, "ph": sec.page_height,
            "ml": sec.left_margin, "mr": sec.right_margin,
            "mt": sec.top_margin, "mb": sec.bottom_margin}


def _tpl_defaults(doc):
    """采样模版 docDefaults：默认中文字体与字号（run 级缺省时兜底）"""
    from docx.oxml.ns import qn
    east, size = None, None
    try:
        dd = doc.styles.element.find(qn("w:docDefaults"))
        if dd is not None:
            rprd = dd.find(qn("w:rPrDefault"))
            if rprd is not None:
                r = rprd.find(qn("w:rPr"))
                if r is not None:
                    rf = r.find(qn("w:rFonts"))
                    if rf is not None:
                        east = rf.get(qn("w:eastAsia"))
                    sz = r.find(qn("w:sz"))
                    if sz is not None:
                        size = int(sz.get(qn("w:val"))) / 2.0
    except Exception:  # noqa: BLE001
        pass
    return {"east": east or "仿宋_GB2312", "size": size or 12.0}


def _tpl_para_fmt(p, dft):
    """采样单个段落：对齐/间距/行距/缩进/字体/字号/加粗（run 级直接格式优先）"""
    from docx.oxml.ns import qn
    fmt = {"jc": None, "before": None, "after": None, "line": None,
           "first_line": None, "east": None, "ascii": None, "size": None, "bold": False}
    pPr = p._element.find(qn("w:pPr"))
    if pPr is not None:
        jc = pPr.find(qn("w:jc"))
        if jc is not None:
            fmt["jc"] = jc.get(qn("w:val"))
        sp = pPr.find(qn("w:spacing"))
        if sp is not None:
            fmt["before"] = sp.get(qn("w:before"))
            fmt["after"] = sp.get(qn("w:after"))
            fmt["line"] = sp.get(qn("w:line"))
        ind = pPr.find(qn("w:ind"))
        if ind is not None:
            fmt["first_line"] = ind.get(qn("w:firstLine"))
    rPr = next((r._element.rPr for r in p.runs if r._element.rPr is not None), None)
    if rPr is not None:
        rf = rPr.find(qn("w:rFonts"))
        if rf is not None:
            fmt["east"] = rf.get(qn("w:eastAsia"))
            fmt["ascii"] = rf.get(qn("w:ascii"))
        sz = rPr.find(qn("w:sz"))
        if sz is not None:
            fmt["size"] = int(sz.get(qn("w:val"))) / 2.0
        b = rPr.find(qn("w:b"))
        if b is not None and b.get(qn("w:val")) in (None, "true", "1"):
            fmt["bold"] = True
    if fmt["east"] is None:
        fmt["east"] = dft["east"]
    if fmt["size"] is None:
        fmt["size"] = dft["size"]
    return fmt


def _clone_tpl_formats(tpl_path):
    """从模版 docx 采样版式：页面 + 标题/章节/正文/落款四类段落格式。
    公文与报告类模版的格式多为段落级直接格式，按特征启发分类采样。"""
    from docx import Document
    try:
        doc = Document(tpl_path)
    except Exception:  # noqa: BLE001
        return None
    F = {"page": None, "title": None, "heading": None,
         "body": None, "body_noind": None, "sign": None}
    try:
        F["page"] = _tpl_section_fmt(doc.sections[0])
    except Exception:  # noqa: BLE001
        pass
    dft = _tpl_defaults(doc)
    paras = [p for p in doc.paragraphs if p.text.strip()]
    if not paras:
        return None
    for i, p in enumerate(paras):
        t = p.text.strip()
        fmt = _tpl_para_fmt(p, dft)
        if F["title"] is None and i < 2 and (
                fmt["jc"] == "center" or fmt["size"] >= dft["size"] + 2):
            F["title"] = fmt                                   # 文档标题（前2段、居中或大字号）
        elif re.match(r"^[一二三四五六七八九十]+、", t) and (
                fmt["bold"] or "黑体" in (fmt["east"] or "")):
            if F["heading"] is None:
                F["heading"] = fmt                             # 章节标题（一、二、…）
        elif fmt["jc"] == "right" and len(t) <= 30:
            if F["sign"] is None:
                F["sign"] = fmt                               # 落款（右对齐短段）
        elif fmt["first_line"] and F["body"] is None:
            F["body"] = fmt                                   # 正文（有首行缩进）
        elif F["body_noind"] is None:
            F["body_noind"] = fmt                             # 无缩进段（主送等）
    if F["body"] is None:
        F["body"] = F["body_noind"] or {"jc": None, "before": None, "after": None,
                                        "line": None, "first_line": None,
                                        "east": dft["east"], "ascii": None,
                                        "size": dft["size"], "bold": False}
    if F["heading"] is None:
        F["heading"] = dict(F["body"], bold=True)
    if F["title"] is None:
        F["title"] = dict(F["heading"])
    return F


def _apply_tpl_fmt(para, run, fmt):
    """把采样到的格式套用到段落与 run"""
    from docx.oxml.ns import qn
    from docx.shared import Pt, Twips
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    pf = para.paragraph_format
    if fmt.get("jc"):
        pf.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER,
                        "right": WD_ALIGN_PARAGRAPH.RIGHT,
                        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(fmt["jc"])
    if fmt.get("before") is not None:
        pf.space_before = Twips(int(fmt["before"]))
    if fmt.get("after") is not None:
        pf.space_after = Twips(int(fmt["after"]))
    if fmt.get("line"):
        pf.line_spacing = int(fmt["line"]) / 240.0            # lineRule=auto：240=单倍
    if fmt.get("first_line"):
        pf.first_line_indent = Twips(int(fmt["first_line"]))
    run.font.name = fmt.get("ascii") or "Times New Roman"     # 建立rFonts并写ascii/hAnsi
    run._element.rPr.rFonts.set(qn("w:eastAsia"), fmt.get("east") or "仿宋_GB2312")
    if fmt.get("size"):
        run.font.size = Pt(fmt["size"])
    if fmt.get("bold"):
        run.font.bold = True


def export_docx_like_template(tpl_path, md_text, fallback_title="仿写文档") -> dict:
    """V1.8.3：按模版版式导出 Word——页面、标题、章节、正文、落款格式克隆自模版；
    文件名取生成正文的一级标题（自然继承模版"关于开展XXX的通知"命名风格）。
    模版不可解析时降级通用导出（export_docx）。"""
    from docx import Document
    F = _clone_tpl_formats(tpl_path)
    if not F:
        return export_docx(fallback_title, md_text)
    blocks = []
    for ln in (md_text or "").split("\n"):
        st = ln.strip()
        if not st:
            continue
        if st.startswith("### "):
            blocks.append(["sec", st[4:].strip()])
        elif st.startswith("## "):
            blocks.append(["sec", st[3:].strip()])
        elif st.startswith("# "):
            blocks.append(["h1", st[2:].strip()])
        else:
            blocks.append(["para", st])
    # 落款识别：结尾处的日期段或短单位段（模版有右对齐落款样式时才生效）
    n = len(blocks)
    for i in range(max(0, n - 3), n):
        k, t = blocks[i]
        if k == "para" and (re.match(r"^\d{4}年", t) or (
                len(t) <= 25 and re.search(r"(委员会|有限公司|分公司|办公室|中心|部)$", t))):
            blocks[i] = ["sign", t]
    title = next((t for k, t in blocks if k == "h1"), None) or fallback_title
    doc = Document()
    if F["page"]:
        sec = doc.sections[0]
        sec.page_width, sec.page_height = F["page"]["pw"], F["page"]["ph"]
        sec.left_margin, sec.right_margin = F["page"]["ml"], F["page"]["mr"]
        sec.top_margin, sec.bottom_margin = F["page"]["mt"], F["page"]["mb"]
    # Normal 样式默认字体对齐模版正文（兜底，未显式套格式的部分仍接近模版）
    try:
        from docx.oxml.ns import qn
        from docx.shared import Pt
        st = doc.styles["Normal"]
        st.font.name = F["body"].get("ascii") or "Times New Roman"
        st.font.size = Pt(F["body"].get("size") or 12)
        st._element.rPr.rFonts.set(qn("w:eastAsia"), F["body"].get("east") or "仿宋_GB2312")
    except Exception:  # noqa: BLE001
        pass
    for k, t in blocks:
        if k == "h1":
            fmt = F["title"]
        elif k == "sec":
            fmt = F["heading"]
        elif k == "sign" and F["sign"]:
            fmt = F["sign"]
        else:
            fmt = F["body"]
        para = doc.add_paragraph()
        run = para.add_run(t)
        _apply_tpl_fmt(para, run, fmt)
    os.makedirs(WRITER_DOCS_DIR, exist_ok=True)
    fname = _safe_name(title, 60) + "_" + datetime.now().strftime("%Y%m%d%H%M%S") + ".docx"
    out = os.path.join(WRITER_DOCS_DIR, fname)
    doc.save(out)
    return {"ok": True, "path": out, "filename": fname, "title": title}


def export_docx(title: str, content: str) -> dict:
    """导出 Word：# / ## / ### → 标题层级，- / 数字列表 → 列表样式，其余为正文段落"""
    from docx import Document
    from docx.shared import Pt
    from docx.oxml.ns import qn

    doc = Document()
    try:
        style = doc.styles["Normal"]
        style.font.name = "Calibri"
        style.font.size = Pt(11)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    except Exception:  # noqa: BLE001
        pass
    title = (title or "").strip()
    if title:
        doc.add_heading(title, 1)
    for line in (content or "").split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        if title and s.strip() == f"# {title}":
            continue  # 与导出标题重复的首行跳过
        if s.startswith("### "):
            doc.add_heading(s[4:].strip(), 3)
        elif s.startswith("## "):
            doc.add_heading(s[3:].strip(), 2)
        elif s.startswith("# "):
            doc.add_heading(s[2:].strip(), 1)
        elif re.match(r"^[-*•]\s+", s):
            doc.add_paragraph(re.sub(r"^[-*•]\s+", "", s), style="List Bullet")
        elif re.match(r"^\d+[.、）)]\s*", s):
            doc.add_paragraph(re.sub(r"^\d+[.、）)]\s*", "", s), style="List Number")
        else:
            doc.add_paragraph(s)
    os.makedirs(WRITER_DOCS_DIR, exist_ok=True)   # V1.7.5：与其他文档同目录，列表统一可见
    fname = _safe_name(title or "写作文档", 40) + "_" + datetime.now().strftime("%Y%m%d%H%M%S") + ".docx"
    out = os.path.join(WRITER_DOCS_DIR, fname)
    doc.save(out)
    return {"ok": True, "path": out, "filename": fname}
