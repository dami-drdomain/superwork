# -*- coding: utf-8 -*-
"""
V1.7.2 Excel 生成引擎（原生可编辑 XLSX）
================================================
与 PPT 版式引擎同一思路：模型只负责内容结构（严格 JSON），排版由本地引擎保证。

每张工作表的固定视觉：
  标题带（主题色，跨列合并） → 说明行（口径/来源） → 表头（主题色+白字+冻结）
  → 斑马纹数据行 + 细边框 + 数字格式（千分位/百分比） → 合计行（真公式 =SUM）
  → 关键列数据条（DataBar） → 数据充分时自动附加原生柱状图。

配色与 PPT 主题一致（政务红金/商务蓝/科技深色/暖色/极简），按专家自动匹配。
"""
from __future__ import annotations

import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

THEMES = {
    "gov":      {"accent": "B91C1C", "deep": "7F1D1D", "accent2": "C08A2E", "soft": "FDF3F3", "line": "E5E7EB"},
    "business": {"accent": "1D4ED8", "deep": "1E3A8A", "accent2": "0EA5E9", "soft": "EFF6FF", "line": "E2E8F0"},
    "tech":     {"accent": "38BDF8", "deep": "0B1220", "accent2": "A78BFA", "soft": "16233D", "line": "1E293B"},
    "warm":     {"accent": "C2410C", "deep": "9A3412", "accent2": "0F766E", "soft": "FDF2E9", "line": "EADFD3"},
    "minimal":  {"accent": "111827", "deep": "374151", "accent2": "10B981", "soft": "F3F4F6", "line": "E5E7EB"},
}
EXPERT_THEME = {"gov": "gov", "tech": "tech", "bid": "business",
                "data": "minimal", "product": "warm", "general": "business"}

NUM_TYPES = ("int", "float", "percent")
TYPE_FORMAT = {"int": "#,##0", "float": "#,##0.00", "percent": "0.0%"}


def _wlen(v) -> int:
    return sum(1 if ord(c) > 0x2E7F else 1 for c in str(v))


def _coerce(value, col_type: str):
    """按列类型转换值：数字列必须是数字，percent 用小数（0.23 = 23%）"""
    if value is None or value == "":
        return None
    if col_type in NUM_TYPES:
        if isinstance(value, (int, float)):
            return value
        t = re.sub(r"[,\s%￥亿元万人辆份个台次项所家]", "", str(value))
        try:
            num = float(t)
            if col_type == "percent" and ("%" in str(value) or abs(num) > 1.5):
                num = num / 100 if "%" in str(value) or abs(num) > 1.5 else num
            return int(num) if col_type == "int" and float(num).is_integer() else num
        except ValueError:
            return str(value)
    return str(value)


def _norm_columns(raw, rows_len: int) -> list:
    """兼容旧格式（columns 为字符串数组）与新格式（对象数组含 type/total/width）"""
    cols = []
    for c in (raw or [])[:10]:
        if isinstance(c, dict):
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            cols.append({"name": name[:20],
                         "type": str(c.get("type") or "text").lower(),
                         "total": bool(c.get("total")),
                         "width": c.get("width")})
        else:
            name = str(c or "").strip()
            if name:
                cols.append({"name": name[:20], "type": "text", "total": False, "width": None})
    # 未声明类型时按内容推断
    for i, col in enumerate(cols):
        if col["type"] in NUM_TYPES or col["type"] == "text":
            continue
        sample = [r[i] for r in rows_len if isinstance(r, list) and i < len(r)][:50]
        nums = sum(1 for v in sample if isinstance(v, (int, float)))
        if sample and nums >= len(sample) * 0.6:
            col["type"] = "percent" if all(isinstance(v, float) and 0 <= v <= 1.5 for v in sample if isinstance(v, (int, float))) else "float"
        else:
            col["type"] = "text"
    return cols


def normalize_spec(spec: dict, expert: str = "general") -> dict:
    spec = spec if isinstance(spec, dict) else {}
    theme_key = str(spec.get("theme") or "").strip().lower()
    theme = theme_key if theme_key in THEMES else EXPERT_THEME.get(expert, "business")
    sheets_out = []
    for sh in (spec.get("sheets") or [])[:5]:
        if not isinstance(sh, dict):
            continue
        rows_raw = [r for r in (sh.get("rows") or []) if isinstance(r, list)][:60]
        cols = _norm_columns(sh.get("columns"), rows_raw)
        if not cols or not rows_raw:
            continue
        rows = []
        for r in rows_raw:
            vals = [_coerce(r[i] if i < len(r) else None, cols[i]["type"]) for i in range(len(cols))]
            rows.append(vals)
        sheets_out.append({
            "name": str(sh.get("name") or "Sheet")[:28] or f"Sheet{len(sheets_out) + 1}",
            "desc": str(sh.get("desc") or "").strip()[:80],
            "columns": cols, "rows": rows,
            "highlight_col": str(sh.get("highlight_col") or "").strip(),
        })
    if not sheets_out:
        sheets_out = [{"name": "说明", "desc": "模型未返回有效表格数据", "columns": [{"name": "项目", "type": "text", "total": False, "width": None}],
                       "rows": [["（请重试或在主题中给出更明确的数据要求）"]], "highlight_col": ""}]
    return {"title": str(spec.get("title") or "数据表")[:60], "meta": str(spec.get("meta") or "").strip()[:60],
            "theme": theme, "sheets": sheets_out}


def _style_sheet(ws, s, theme, title, meta, multi=False):
    t = THEMES[theme]
    n = len(s["columns"])
    last_col = get_column_letter(n)
    thin = Side(style="thin", color=t["line"])
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 标题带
    ws.merge_cells(f"A1:{last_col}1")
    c = ws.cell(row=1, column=1, value=title + ("｜" + s["name"] if multi else ""))
    c.font = Font(bold=True, size=14, color="FFFFFF", name="微软雅黑")
    c.fill = PatternFill("solid", fgColor=t["accent"])
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 30
    # 说明行
    note = s["desc"] or "数据口径见正文"
    if meta:
        note = f"{note}　·　{meta}" if note else meta
    ws.merge_cells(f"A2:{last_col}2")
    c2 = ws.cell(row=2, column=1, value=note)
    c2.font = Font(size=9, color="6B7280", name="微软雅黑")
    c2.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 16
    # 表头（第 4 行，留 1 行呼吸）
    head_row = 4
    for i, col in enumerate(s["columns"], 1):
        h = ws.cell(row=head_row, column=i, value=col["name"])
        h.font = Font(bold=True, size=11, color="FFFFFF", name="微软雅黑")
        h.fill = PatternFill("solid", fgColor=t["deep"])
        h.alignment = Alignment(horizontal="center", vertical="center")
        h.border = border
    ws.row_dimensions[head_row].height = 22
    ws.freeze_panes = f"A{head_row + 1}"

    # 数据行
    first_data = head_row + 1
    for ri, row in enumerate(s["rows"]):
        r = first_data + ri
        zebra = ri % 2 == 1
        for ci, val in enumerate(row, 1):
            cell = ws.cell(row=r, column=ci, value=val)
            col = s["columns"][ci - 1]
            cell.border = border
            cell.font = Font(size=10.5, name="微软雅黑", color="1F2937")
            if col["type"] in NUM_TYPES and isinstance(val, (int, float)):
                cell.number_format = TYPE_FORMAT[col["type"]]
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            if zebra:
                cell.fill = PatternFill("solid", fgColor=t["soft"])

    # 合计行（真公式）
    total_cols = [i for i, c in enumerate(s["columns"], 1) if c["total"] and c["type"] in NUM_TYPES]
    last_data = first_data + len(s["rows"]) - 1
    if total_cols and len(s["rows"]) >= 2:
        tr = last_data + 1
        label = ws.cell(row=tr, column=1, value="合计")
        label.font = Font(bold=True, size=11, name="微软雅黑", color=t["deep"])
        label.alignment = Alignment(horizontal="center", vertical="center")
        label.border = border
        for ci in range(2, n + 1):
            ws.cell(row=tr, column=ci).border = border
        for ci in total_cols:
            letter = get_column_letter(ci)
            fc = ws.cell(row=tr, column=ci, value=f"=SUM({letter}{first_data}:{letter}{last_data})")
            fc.font = Font(bold=True, size=10.5, name="微软雅黑", color=t["deep"])
            fc.number_format = TYPE_FORMAT[s["columns"][ci - 1]["type"]]
            fc.alignment = Alignment(horizontal="right", vertical="center")
            fc.border = border
        for ci in range(1, n + 1):
            ws.cell(row=tr, column=ci).fill = PatternFill("solid", fgColor=t["soft"])

    # 列宽：内容自适应（中文按 2 计），受 width 提示约束
    for ci, col in enumerate(s["columns"], 1):
        content_max = _wlen(col["name"]) * 1.9
        for row in s["rows"]:
            v = row[ci - 1] if ci - 1 < len(row) else None
            if v is not None:
                content_max = max(content_max, _wlen(v) * 1.15)
        hint = col.get("width")
        width = min(max(9, content_max), 42)
        if isinstance(hint, (int, float)) and 8 <= hint <= 42:
            width = hint
        ws.column_dimensions[get_column_letter(ci)].width = width
    return head_row, first_data, last_data


def _add_databar(ws, s, first_data, last_data, theme):
    hl = s.get("highlight_col")
    if not hl:
        return
    for ci, col in enumerate(s["columns"], 1):
        if col["name"] == hl and col["type"] in NUM_TYPES:
            letter = get_column_letter(ci)
            rule = DataBarRule(start_type="num", start_value=0, end_type="max",
                               color=THEMES[theme]["accent"], showValue=True)
            ws.conditional_formatting.add(f"{letter}{first_data}:{letter}{last_data}", rule)
            break


def _add_chart(ws, s, head_row, first_data, last_data, theme):
    """数据充分时在表格右侧放一张原生柱状图（类别取第一列文本，值取首个数值列）"""
    if len(s["rows"]) < 4 or len(s["columns"]) > 9:
        return
    text_ci = next((i for i, c in enumerate(s["columns"], 1) if c["type"] == "text"), None)
    num_ci = next((i for i, c in enumerate(s["columns"], 1) if c["type"] in NUM_TYPES), None)
    if not text_ci or not num_ci:
        return
    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = f'{s["columns"][num_ci - 1]["name"]}（按{s["columns"][text_ci - 1]["name"]}）'
    data = Reference(ws, min_col=num_ci, min_row=head_row, max_row=last_data)
    cats = Reference(ws, min_col=text_ci, min_row=first_data, max_row=last_data)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.height, chart.width = 7.2, 15
    chart.gapWidth = 60
    try:
        chart.series[0].graphicalProperties.solidFill = THEMES[theme]["accent"]
    except Exception:  # noqa: BLE001
        pass
    anchor = f'{get_column_letter(len(s["columns"]) + 2)}{head_row}'
    ws.add_chart(chart, anchor)


def build_workbook(spec: dict, path: str, expert: str = "general") -> dict:
    norm = normalize_spec(spec, expert)
    wb = Workbook()
    wb.remove(wb.active)
    meta = norm.get("meta") or datetime.now().strftime("生成于 %Y-%m-%d %H:%M")
    for s in norm["sheets"]:
        ws = wb.create_sheet(re.sub(r"[\\/:*?\[\]]", " ", s["name"])[:28] or "Sheet")
        head_row, first_data, last_data = _style_sheet(ws, s, norm["theme"], norm["title"], meta, multi=len(norm["sheets"]) > 1)
        _add_databar(ws, s, first_data, last_data, norm["theme"])
        _add_chart(ws, s, head_row, first_data, last_data, norm["theme"])
    if not wb.sheetnames:
        wb.create_sheet("Sheet1")
    wb.save(path)
    return {"sheets": len(wb.sheetnames), "theme": norm["theme"],
            "rows": sum(len(s["rows"]) for s in norm["sheets"])}


def workbook_from_text(text: str, title: str = "", expert: str = "general") -> dict:
    """兜底：模型未返回合法 JSON 时，从 Markdown 表格（| 分隔）或纯文本行抢救"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    table_rows = []
    for ln in lines:
        if ln.startswith("|") and ln.count("|") >= 2:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            table_rows.append(cells)
    if len(table_rows) >= 2:
        cols = table_rows[0]
        rows = [r + [""] * (len(cols) - len(r)) for r in table_rows[1:]]
        return {"title": title or "数据表", "theme": EXPERT_THEME.get(expert, "business"),
                "sheets": [{"name": "数据", "columns": cols, "rows": rows}]}
    plain = []
    for x in lines:
        x = re.sub(r"^[-*•\d.\s]+", "", x)
        if len(x) > 380:                       # 超长单行（如压缩 JSON）按对象边界切开展示
            for piece in re.split(r'(?=\{")', x):
                piece = piece.strip().rstrip(",")
                if piece:
                    plain.append(piece[:160])
        elif x:
            plain.append(x)
    plain = plain[:60]
    return {"title": title or "数据表", "theme": EXPERT_THEME.get(expert, "business"),
            "sheets": [{"name": "内容", "columns": ["序号", "内容"],
                        "rows": [[i + 1, p[:60]] for i, p in enumerate(plain) if p]}]}
