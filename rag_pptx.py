# -*- coding: utf-8 -*-
"""
V1.7.0 演示文稿生成引擎（原生可编辑 PPTX）
================================================
设计思路：把「PPT 质量」拆成三件事，逐一用代码保证，而不是交给模型自由发挥：

1) 版式（layout）：10 种版式（封面/章节/要点/KPI/图表/流程/对比/表格/引用/结尾），
   每种版式固定栅格与字号层级，避免“一页塞满字”；
2) 视觉（theme）：5 套配色与字体系统，按专家自动匹配（政务红金/科技深色/商务蓝/暖色/极简）；
3) 适配（fit）：按文本量估算行数并自动降字号/分栏，杜绝溢出。

输出为 python-pptx 原生对象（文本框、形状、原生图表、表格），可在 PowerPoint/WPS 中继续编辑，
不是图片拼贴。所有版式均为 16:9。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

# ---------------- 画布与栅格 ----------------
SW, SH = 13.333, 7.5          # 16:9（英寸）
M = 0.78                      # 左右页边距
CW = SW - 2 * M               # 内容宽度
TOP = 0.62                    # 标题基线
BODY_TOP = 1.95               # 正文起始
FOOT_Y = 6.95

FONT = "微软雅黑"              # 目标环境 Windows 云电脑自带；Mac 亦可用
FONT_EN = "Arial"

# ---------------- 主题 ----------------
THEMES = {
    "gov": {          # 政务汇报：白底红金
        "bg": "FFFFFF", "ink": "1F2937", "muted": "6B7280", "line": "E5E7EB",
        "accent": "B91C1C", "accent2": "C08A2E", "soft": "FDF3F3", "deep": "7F1D1D",
        "dark": False,
    },
    "business": {     # 商务方案：白底深蓝
        "bg": "FFFFFF", "ink": "0F172A", "muted": "64748B", "line": "E2E8F0",
        "accent": "1D4ED8", "accent2": "0EA5E9", "soft": "EFF6FF", "deep": "1E3A8A",
        "dark": False,
    },
    "tech": {         # 科技产品：深色
        "bg": "0B1220", "ink": "E5E7EB", "muted": "94A3B8", "line": "1E293B",
        "accent": "38BDF8", "accent2": "A78BFA", "soft": "12203A", "deep": "0EA5E9",
        "dark": True,
    },
    "warm": {         # 产品运营：暖白
        "bg": "FFFBF5", "ink": "2B2118", "muted": "8A7563", "line": "EADFD3",
        "accent": "C2410C", "accent2": "0F766E", "soft": "FDF2E9", "deep": "9A3412",
        "dark": False,
    },
    "minimal": {      # 数据分析：极简黑白
        "bg": "FFFFFF", "ink": "111827", "muted": "6B7280", "line": "E5E7EB",
        "accent": "111827", "accent2": "10B981", "soft": "F3F4F6", "deep": "374151",
        "dark": False,
    },
}

EXPERT_THEME = {
    "gov": "gov", "tech": "tech", "bid": "business",
    "data": "minimal", "product": "warm", "general": "business",
}
PALETTE = ["accent", "accent2", "deep", "muted"]

CHART_TYPES = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "hbar": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
    "donut": XL_CHART_TYPE.DOUGHNUT,
    "area": XL_CHART_TYPE.AREA,
}


def rgb(hex_str: str) -> RGBColor:
    h = (hex_str or "000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ---------------- 基础绘制 ----------------
def _set_ea_font(run, font_name: str):
    """设置中文（东亚）字体，避免只生效于英文而中文回退宋体"""
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        latin = rPr.find(qn("a:latin"))
        if latin is not None:
            latin.addnext(ea)
        else:
            rPr.append(ea)
    ea.set("typeface", font_name)


def add_text(slide, x, y, w, h, text, size=16, bold=False, color="1F2937",
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, font=FONT,
             line_spacing=1.22, space_after=0, italic=False):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line_spacing
        p.space_after = Pt(space_after)
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = rgb(color)
        r.font.name = font if not re.search(r"[A-Za-z]", line) else FONT_EN
        _set_ea_font(r, font)
    return box


def add_rect(slide, x, y, w, h, fill=None, line=None, radius=None, shadow=False):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius is not None else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    if radius is not None:
        try:
            shp.adjustments[0] = radius
        except Exception:  # noqa: BLE001
            pass
    if fill:
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(fill)
    else:
        shp.fill.background()
    if line:
        shp.line.color.rgb = rgb(line)
        shp.line.width = Pt(1)
    else:
        shp.line.fill.background()
    if not shadow:
        shp.shadow.inherit = False
    shp.text_frame.text = ""
    return shp


def add_oval(slide, x, y, d, fill, line=None):
    shp = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(d), Inches(d))
    shp.fill.solid()
    shp.fill.fore_color.rgb = rgb(fill)
    if line:
        shp.line.color.rgb = rgb(line)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    shp.text_frame.text = ""
    return shp


def add_line(slide, x1, y1, x2, y2, color, width=1.0):
    ln = slide.shapes.add_connector(1, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    ln.line.color.rgb = rgb(color)
    ln.line.width = Pt(width)
    return ln


def _bg(slide, theme):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = rgb(theme["bg"])


def _footer(slide, ctx, page):
    t = ctx["theme"]
    add_text(slide, M, FOOT_Y, CW * 0.75, 0.3, ctx["deck_title"], 9, color=t["muted"])
    add_text(slide, SW - M - 1.2, FOOT_Y, 1.2, 0.3, f"{page:02d} / {ctx['total']:02d}",
             9, color=t["muted"], align=PP_ALIGN.RIGHT, font=FONT_EN)
    add_line(slide, M, FOOT_Y - 0.12, SW - M, FOOT_Y - 0.12, t["line"], 0.75)


def _title(slide, s, ctx, kicker=None):
    """标准页标题区：kicker + 标题 + 强调短线；返回正文可用顶部 y"""
    t = ctx["theme"]
    y = TOP
    if kicker:
        add_text(slide, M, y, CW, 0.28, str(kicker).upper(), 10.5, True, t["accent"])
        y += 0.34
    add_text(slide, M, y, CW, 0.62, str(s.get("title") or "未命名页"), 27, True, t["ink"])
    add_rect(slide, M, y + 0.68, 0.9, 0.055, t["accent"])
    sub = str(s.get("subtitle") or "").strip()
    if sub:
        add_text(slide, M, y + 0.82, CW * 0.92, 0.34, sub, 13, False, t["muted"])
        return y + 1.3
    return y + 0.95


def _notes(slide, s):
    notes = str(s.get("notes") or "").strip()
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


# ---------------- 文本适配 ----------------
def _wlen(text: str) -> float:
    """加权字宽：中文/全角 1.0，英文数字 0.55，用于估算换行"""
    n = 0.0
    for ch in str(text):
        n += 1.0 if ord(ch) > 0x2E7F else 0.55
    return n


def _est_lines(text: str, size: float, width_in: float) -> int:
    per_line = max(6.0, width_in * 72.0 / (size * 1.02))
    return max(1, math.ceil(_wlen(text) / per_line))


def _fit_size(items, base, minimum, width_in, height_in, spacing_pt=8):
    size = base
    while size > minimum:
        lines = sum(_est_lines(x, size, width_in) for x in items)
        need = lines * size * 1.42 + spacing_pt * max(0, len(items) - 1)
        if need <= height_in * 72:
            return size
        size -= 1
    return minimum


def _bullet_block(slide, items, x, y, w, h, theme, base=18, minimum=13, columns_if_many=7):
    items = [str(i).strip() for i in items if str(i).strip()]
    if not items:
        return
    two_col = len(items) >= columns_if_many
    col_gap = 0.42
    if two_col:
        half = math.ceil(len(items) / 2)
        groups = [(items[:half], x, (w - col_gap) / 2), (items[half:], x + (w - col_gap) / 2 + col_gap, (w - col_gap) / 2)]
    else:
        groups = [(items, x, w)]
    for grp, gx, gw in groups:
        size = _fit_size(grp, base, minimum, gw - 0.4, h)
        box = slide.shapes.add_textbox(Inches(gx), Inches(y), Inches(gw), Inches(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        for i, item in enumerate(grp):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.line_spacing = 1.3
            p.space_after = Pt(9 if size >= 16 else 7)
            mark = p.add_run()
            mark.text = "▍ "
            mark.font.size = Pt(size)
            mark.font.bold = True
            mark.font.color.rgb = rgb(theme["accent"])
            mark.font.name = FONT
            _set_ea_font(mark, FONT)
            run = p.add_run()
            run.text = item
            run.font.size = Pt(size)
            run.font.color.rgb = rgb(theme["ink"])
            run.font.name = FONT
            _set_ea_font(run, FONT)


# ---------------- 各种版式 ----------------
def slide_cover(prs, s, ctx):
    """封面 V2：左侧竖向色带 + 大标题 + 细分割线，去掉无意义几何装饰（政务/商务通用）"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    # 右侧深色竖版块：承担视觉配重，正式场合常用（不放几何装饰与英文）
    panel_w = 4.15
    add_rect(sl, SW - panel_w, 0, panel_w, SH, t["deep"])
    add_rect(sl, SW - panel_w - 0.05, 0, 0.05, SH, t["accent2"])
    # 文字组
    x = 1.12
    kicker = str(s.get("kicker") or ctx.get("kicker") or "汇报材料")
    add_text(sl, x, 1.42, 7.2, 0.32, kicker, 11.5, True, t["muted"])
    add_rect(sl, x, 1.80, 0.62, 0.05, t["accent2"])
    add_text(sl, x, 2.2, 7.35, 1.95, str(s.get("title") or ctx["deck_title"]), 40, True, t["ink"], line_spacing=1.14)
    add_rect(sl, x, 4.18, 1.05, 0.055, t["accent"])
    sub = str(s.get("subtitle") or "")
    if sub:
        add_text(sl, x, 4.5, 7.0, 1.0, sub, 17, False, t["muted"], line_spacing=1.35)
    # 底部信息与分割线
    add_line(sl, x, 6.28, SW - panel_w - 0.3, 6.28, t["line"], 0.9)
    meta = str(s.get("meta") or ctx.get("meta") or datetime.now().strftime("%Y 年 %m 月"))
    add_text(sl, x, 6.42, 7.6, 0.34, meta, 11, False, t["muted"])
    tag = str(s.get("tag") or "内部资料")
    add_text(sl, SW - panel_w + 0.35, 6.42, panel_w - 0.7, 0.34, tag, 10.5, False, "FFFFFF", align=PP_ALIGN.LEFT)
    _notes(sl, s)


def slide_section(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    idx = str(s.get("index") or ctx.get("section_no") or "")
    if idx:
        add_text(sl, M - 0.05, 1.05, 3.6, 2.2, idx.zfill(2), 96, True, t["soft"], font=FONT_EN)
    add_text(sl, M, 3.25, CW * 0.8, 1.0, str(s.get("title") or ""), 34, True, t["ink"])
    add_rect(sl, M, 4.28, 1.1, 0.06, t["accent"])
    sub = str(s.get("subtitle") or "").strip()
    if sub:
        add_text(sl, M, 4.55, CW * 0.75, 0.9, sub, 14, False, t["muted"], line_spacing=1.35)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_bullets(prs, s, ctx):
    """要点页 V1.7.1：≤6 条时改为编号圆点 + 分隔线的行式排版，更像设计稿而非清单"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    items = [str(x).strip() for x in (s.get("bullets") or []) if str(x).strip()]
    body_h = SH - top - 0.95
    row_mode = 2 <= len(items) <= 6 and all(_wlen(x) <= 52 for x in items)
    if row_mode:
        n = len(items)
        row_h = body_h / n
        y = top + 0.2
        for i, item in enumerate(items):
            cy = y + i * row_h
            d = min(0.42, row_h * 0.5)
            add_oval(sl, M, cy + row_h / 2 - d / 2, d, t["accent"] if i % 2 == 0 else t["accent2"])
            add_text(sl, M, cy + row_h / 2 - d / 2 + 0.05, d, d * 0.6, str(i + 1),
                     12.5, True, "FFFFFF", align=PP_ALIGN.CENTER, font=FONT_EN)
            size = 16 if _wlen(item) <= 34 else 14.5
            add_text(sl, M + 0.66, cy + row_h / 2 - 0.24, CW - 0.9, 0.6, item, size, False, t["ink"],
                     line_spacing=1.3)
            if i < n - 1:
                add_line(sl, M + 0.66, cy + row_h, SW - M, cy + row_h, t["line"], 0.75)
    else:
        _bullet_block(sl, items, M, top + 0.25, CW, body_h, t)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_kpi(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    items = [i for i in (s.get("items") or []) if isinstance(i, dict)][:4]
    if not items:
        items = [{"value": "—", "label": "暂无数据"}]
    n = len(items)
    gap = 0.28
    card_w = (CW - gap * (n - 1)) / n
    cy = top + 0.55
    ch = 2.55 if n <= 3 else 2.3
    for i, it in enumerate(items):
        x = M + i * (card_w + gap)
        add_rect(sl, x, cy, card_w, ch, t["soft"], radius=0.06)
        add_rect(sl, x, cy, card_w, 0.07, t["accent"] if i % 2 == 0 else t["accent2"])
        add_text(sl, x + 0.26, cy + 0.4, card_w - 0.5, 0.95,
                 str(it.get("value") or "—"), 34 if n <= 3 else 30, True,
                 t["accent"] if i % 2 == 0 else t["accent2"], font=FONT_EN)
        add_text(sl, x + 0.26, cy + 1.38, card_w - 0.5, 0.5, str(it.get("label") or ""), 13, True, t["ink"])
        note = str(it.get("note") or "").strip()
        if note:
            add_text(sl, x + 0.26, cy + 1.85, card_w - 0.5, 0.45, note, 10.5, False, t["muted"])
    insight = str(s.get("insight") or "").strip()
    if insight:
        add_text(sl, M, cy + ch + 0.32, CW, 0.4, "结论：" + insight, 12, False, t["muted"])
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def _style_chart(chart, s, theme):
    kind = str(s.get("chart_type") or "bar").lower()
    chart.has_title = False
    chart.font.size = Pt(10.5)
    chart.font.name = FONT
    chart.font.color.rgb = rgb(theme["muted"])
    multi = len((s.get("series") or [])) > 1
    chart.has_legend = multi
    if multi:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(10)
    plot = chart.plots[0]
    try:
        if kind in ("bar", "column", "hbar"):
            plot.gap_width = 55
            plot.overlap = -8 if multi else 0
        if kind in ("bar", "column", "hbar", "line", "area") and not multi:
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.font.size = Pt(10)
            dl.font.bold = True
            dl.font.color.rgb = rgb(theme["ink"])
            dl.position = XL_LABEL_POSITION.OUTSIDE_END
        if kind in ("pie", "donut"):
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.show_percentage = True
            dl.show_value = False
            dl.font.size = Pt(10.5)
            dl.font.color.rgb = rgb(theme["bg"])
    except Exception:  # noqa: BLE001
        pass
    # 轴与网格
    try:
        va = chart.value_axis
        va.has_major_gridlines = kind not in ("pie", "donut")
        if va.has_major_gridlines:
            va.major_gridlines.format.line.color.rgb = rgb(theme["line"])
            va.major_gridlines.format.line.width = Pt(0.75)
        va.tick_labels.font.size = Pt(10)
        va.tick_labels.font.color.rgb = rgb(theme["muted"])
        va.format.line.color.rgb = rgb(theme["line"])
        ca = chart.category_axis
        ca.tick_labels.font.size = Pt(10.5)
        ca.tick_labels.font.color.rgb = rgb(theme["ink"])
        ca.format.line.color.rgb = rgb(theme["line"])
    except Exception:  # noqa: BLE001
        pass
    # 配色
    colors = [theme["accent"], theme["accent2"], theme["deep"], theme["muted"]]
    for si, series in enumerate(chart.series):
        try:
            if kind in ("pie", "donut"):
                for pi, point in enumerate(series.points):
                    point.format.fill.solid()
                    point.format.fill.fore_color.rgb = rgb(colors[pi % len(colors)])
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = rgb(colors[si % len(colors)])
                if kind in ("line", "area"):
                    series.format.line.color.rgb = rgb(colors[si % len(colors)])
                    series.format.line.width = Pt(2.25)
        except Exception:  # noqa: BLE001
            pass


def slide_chart(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    cats = [str(c) for c in (s.get("categories") or [])][:12]
    series = [x for x in (s.get("series") or []) if isinstance(x, dict)][:4]
    if not cats or not series:
        os_text = str(s.get("insight") or s.get("subtitle") or "（缺少图表数据）")
        add_text(sl, M, top + 0.6, CW, 1.2, os_text, 16, False, t["muted"])
        _footer(sl, ctx, ctx["page"])
        return
    data = CategoryChartData()
    data.categories = cats
    for sr in series:
        vals = []
        for v in (sr.get("values") or [])[:len(cats)]:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                vals.append(0.0)
        vals += [0.0] * (len(cats) - len(vals))
        data.add_series(str(sr.get("name") or "系列"), vals)
    kind = str(s.get("chart_type") or "bar").lower()
    chart_type = CHART_TYPES.get(kind, XL_CHART_TYPE.COLUMN_CLUSTERED)
    chart_h = 4.35 if str(s.get("insight") or "").strip() else 4.75
    gframe = sl.shapes.add_chart(chart_type, Inches(M), Inches(top + 0.2),
                                 Inches(CW), Inches(chart_h), data)
    _style_chart(gframe.chart, s, t)
    insight = str(s.get("insight") or "").strip()
    if insight:
        add_text(sl, M, top + 0.2 + chart_h + 0.12, CW, 0.4, "结论：" + insight, 12, False, t["muted"])
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_process(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    steps = [str(x) for x in (s.get("steps") or []) if str(x).strip()][:6]
    if not steps:
        _footer(sl, ctx, ctx["page"])
        return
    n = len(steps)
    if n <= 4:
        gap = 0.06
        w = (CW - gap * (n - 1)) / n
        y = top + 1.15
        for i, st in enumerate(steps):
            x = M + i * (w + gap)
            shp = sl.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(x), Inches(y), Inches(w), Inches(1.15))
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(t["accent"] if i % 2 == 0 else t["deep"])
            shp.line.fill.background()
            shp.shadow.inherit = False
            tf = shp.text_frame
            tf.word_wrap = True
            tf.margin_left = Inches(0.18)
            tf.margin_right = Inches(0.12)
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = st
            r.font.size = Pt(12.5 if len(st) <= 16 else 11.5)
            r.font.bold = True
            r.font.color.rgb = rgb("FFFFFF")
            r.font.name = FONT
            _set_ea_font(r, FONT)
        desc = str(s.get("subtitle") or "").strip()
        if desc:
            add_text(sl, M, y + 1.55, CW, 1.4, desc, 13, False, t["muted"], line_spacing=1.45)
    else:
        y = top + 0.35
        row_h = min(0.95, (SH - y - 0.85) / n)
        for i, st in enumerate(steps):
            yy = y + i * row_h
            add_oval(sl, M, yy + 0.12, 0.46, t["accent"] if i % 2 == 0 else t["deep"])
            add_text(sl, M, yy + 0.18, 0.46, 0.3, str(i + 1), 12, True, "FFFFFF",
                     align=PP_ALIGN.CENTER, font=FONT_EN)
            add_text(sl, M + 0.68, yy + 0.16, CW - 0.8, row_h - 0.2, st, 14, False, t["ink"])
            if i < n - 1:
                add_line(sl, M + 0.23, yy + 0.6, M + 0.23, yy + row_h + 0.1, t["line"], 1.25)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_compare(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    panes = [s.get("left") or {}, s.get("right") or {}]
    gap = 0.34
    pw = (CW - gap) / 2
    ph = SH - top - 1.15
    for i, pane in enumerate(panes):
        x = M + i * (pw + gap)
        accent = t["accent"] if i == 0 else t["accent2"]
        add_rect(sl, x, top + 0.2, pw, ph, t["soft"] if i == 0 else t["bg"], radius=0.05)
        add_rect(sl, x, top + 0.2, pw, 0.62, accent, radius=0.05)
        add_text(sl, x + 0.25, top + 0.32, pw - 0.5, 0.4, str(pane.get("title") or ("方案 A" if i == 0 else "方案 B")),
                 15, True, "FFFFFF")
        items = [str(x2) for x in [pane.get("items") or []] for x2 in x][:6]
        if items:
            _bullet_block(sl, items, x + 0.28, top + 1.02, pw - 0.56, ph - 1.0, t, base=14, minimum=11, columns_if_many=99)
    add_line(sl, M + pw + gap / 2, top + 0.35, M + pw + gap / 2, top + ph, t["line"], 1)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_table(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    cols = [str(c) for c in (s.get("columns") or [])][:6]
    rows = [[str(v) for v in r][:len(cols)] for r in (s.get("rows") or [])][:8]
    if not cols or not rows:
        add_text(sl, M, top + 0.5, CW, 1, "（缺少表格数据）", 14, False, t["muted"])
        _footer(sl, ctx, ctx["page"])
        return
    n_rows, n_cols = len(rows) + 1, len(cols)
    height = min(SH - top - 1.05, 0.46 * n_rows + 0.2)
    gf = sl.shapes.add_table(n_rows, n_cols, Inches(M), Inches(top + 0.22), Inches(CW), Inches(height))
    table = gf.table
    for ci in range(n_cols):
        table.columns[ci].width = Emu(int(Inches(CW) / n_cols))
    for ci, c in enumerate(cols):
        cell = table.cell(0, ci)
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(t["accent"])
        cell.margin_left = cell.margin_right = Inches(0.08)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = cell.text_frame.paragraphs[0]
        r = p.add_run()
        r.text = c
        r.font.size = Pt(12)
        r.font.bold = True
        r.font.color.rgb = rgb("FFFFFF")
        r.font.name = FONT
        _set_ea_font(r, FONT)
    for ri, row in enumerate(rows, 1):
        for ci in range(n_cols):
            cell = table.cell(ri, ci)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(t["bg"] if ri % 2 else t["soft"])
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = cell.text_frame.paragraphs[0]
            r = p.add_run()
            r.text = row[ci] if ci < len(row) else ""
            r.font.size = Pt(11.5)
            r.font.color.rgb = rgb(t["ink"])
            r.font.name = FONT
            _set_ea_font(r, FONT)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_quote(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    add_text(sl, M, 1.0, 1.3, 1.62, "“", 84, True, t["soft"], font=FONT_EN)
    add_text(sl, M + 1.25, 1.55, CW - 2.2, 2.9, str(s.get("text") or s.get("title") or ""), 24, True, t["ink"], line_spacing=1.42)
    add_rect(sl, M + 1.3, 4.75, 0.9, 0.05, t["accent"])
    src = str(s.get("source") or "").strip()
    if src:
        add_text(sl, M + 1.3, 4.98, CW - 2.2, 0.4, "— " + src, 12, False, t["muted"])
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_end(prs, s, ctx):
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    add_rect(sl, 0, 0, SW, 0.12, t["accent"])
    add_text(sl, 0, 2.55, SW, 1.1, str(s.get("title") or "谢谢"), 42, True, t["ink"], align=PP_ALIGN.CENTER)
    add_rect(sl, SW / 2 - 0.45, 3.78, 0.9, 0.06, t["accent"])
    sub = str(s.get("subtitle") or "敬请批评指正").strip()
    add_text(sl, 0, 4.05, SW, 0.5, sub, 16, False, t["muted"], align=PP_ALIGN.CENTER)
    contact = str(s.get("contact") or "").strip()
    if contact:
        add_text(sl, 0, 4.75, SW, 0.4, contact, 11.5, False, t["muted"], align=PP_ALIGN.CENTER)
    _notes(sl, s)


def slide_timeline(prs, s, ctx):
    """时间轴：中线 + 事件点 + 上下交替标注（V1.7.1 新增）"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    events = [e for e in (s.get("events") or []) if isinstance(e, dict)][:6]
    if not events:
        _footer(sl, ctx, ctx["page"])
        _notes(sl, s)
        return
    n = len(events)
    axis_y = top + 2.55
    x0, x1 = M + 0.35, SW - M - 0.35
    add_line(sl, x0, axis_y, x1, axis_y, t["line"], 2.0)
    add_line(sl, x0, axis_y, x0 + 0.22, axis_y - 0.09, t["accent"], 2.0)
    add_oval(sl, x1 - 0.02, axis_y - 0.07, 0.14, t["accent"])
    slot = (x1 - x0) / n
    for i, ev in enumerate(events):
        cx = x0 + slot * (i + 0.5)
        date = str(ev.get("date") or ev.get("label") or f"{i + 1}")[:14]
        text = str(ev.get("text") or "")[:60]
        up = i % 2 == 0
        add_oval(sl, cx - 0.075, axis_y - 0.075, 0.15, t["accent"] if up else t["accent2"])
        add_line(sl, cx, axis_y + (-0.32 if up else 0.32), cx, axis_y + (-0.14 if up else 0.14), t["line"], 1.0)
        bw = min(slot - 0.18, 2.4)
        by = axis_y - 1.85 if up else axis_y + 0.45
        add_rect(sl, cx - bw / 2, by, bw, 1.35, t["soft"], radius=0.08)
        add_text(sl, cx - bw / 2 + 0.16, by + 0.14, bw - 0.32, 0.3, date, 12, True,
                 t["accent"] if up else t["accent2"])
        add_text(sl, cx - bw / 2 + 0.16, by + 0.5, bw - 0.32, 0.75, text, 11, False, t["ink"], line_spacing=1.25)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_layers(prs, s, ctx):
    """层级/架构：由上至下的分层条带，宽度递增（V1.7.1 新增）"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    layers = [l for l in (s.get("layers") or []) if isinstance(l, dict)][:5]
    if not layers:
        _footer(sl, ctx, ctx["page"])
        _notes(sl, s)
        return
    n = len(layers)
    area_h = SH - top - 1.15
    gap = 0.14
    bar_h = min(0.92, (area_h - gap * (n - 1)) / n)
    y = top + 0.3
    for i, lay in enumerate(layers):
        frac = 0.62 + 0.38 * (i / max(1, n - 1)) if n > 1 else 1.0
        w = CW * frac
        x = M + (CW - w) / 2
        add_rect(sl, x, y, w, bar_h, t["soft"] if i % 2 == 0 else t["bg"], line=t["line"], radius=0.1)
        add_rect(sl, x, y, 0.62, bar_h, t["accent"] if i % 2 == 0 else t["deep"])
        add_text(sl, x + 0.08, y + bar_h / 2 - 0.16, 0.46, 0.3, str(i + 1), 13, True, "FFFFFF",
                 align=PP_ALIGN.CENTER, font=FONT_EN)
        name = str(lay.get("name") or lay.get("title") or "")[:22]
        add_text(sl, x + 0.78, y + 0.12, w - 1.0, 0.34, name, 13.5, True, t["ink"])
        desc = str(lay.get("desc") or lay.get("subtitle") or "")[:70]
        if desc:
            add_text(sl, x + 0.78, y + 0.48, w - 1.0, 0.32, desc, 10.5, False, t["muted"])
        if i < n - 1:
            cxp = M + CW / 2
            add_line(sl, cxp, y + bar_h + 0.02, cxp, y + bar_h + gap - 0.02, t["accent2"], 1.25)
        y += bar_h + gap
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_matrix(prs, s, ctx):
    """四象限矩阵：2×2 卡片（V1.7.1 新增）"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    quads = [q for q in (s.get("quadrants") or []) if isinstance(q, dict)][:4]
    if not quads:
        _footer(sl, ctx, ctx["page"])
        _notes(sl, s)
        return
    while len(quads) < 4:
        quads.append({"label": "", "items": []})
    gap = 0.24
    cw2 = (CW - gap) / 2
    ch2 = (SH - top - 1.2 - gap) / 2
    colors = [t["accent"], t["accent2"], t["deep"], t["muted"]]
    for i, q in enumerate(quads):
        x = M + (i % 2) * (cw2 + gap)
        y = top + 0.25 + (i // 2) * (ch2 + gap)
        add_rect(sl, x, y, cw2, ch2, t["soft"], radius=0.06)
        add_rect(sl, x, y, cw2, 0.52, colors[i], radius=0.06)
        add_text(sl, x + 0.22, y + 0.1, cw2 - 0.44, 0.32, str(q.get("label") or "")[:20], 13, True, "FFFFFF")
        items = [str(v) for v in (q.get("items") or [])][:3]
        if items:
            _bullet_block(sl, items, x + 0.24, y + 0.68, cw2 - 0.48, ch2 - 0.85, t,
                          base=12.5, minimum=10, columns_if_many=99)
    add_text(sl, M + cw2 + gap / 2 - 0.25, top + 0.25 + ch2 + gap / 2 - 0.18, 0.5, 0.36, "VS",
             11, True, t["bg"], align=PP_ALIGN.CENTER, font=FONT_EN)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_hub(prs, s, ctx):
    """中心辐射：中枢 + 外围节点（V1.7.1 新增）"""
    import math as _m
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    top = _title(sl, s, ctx, s.get("kicker"))
    nodes = [x for x in (s.get("nodes") or []) if isinstance(x, dict)][:6]
    center = str(s.get("center") or "核心")[:16]
    if not nodes:
        _footer(sl, ctx, ctx["page"])
        _notes(sl, s)
        return
    cx, cy = SW / 2, top + 2.75
    rx, ry = CW / 2 - 1.45, 1.95
    nw, nh = 2.35, 0.92
    pts = []
    for i in range(len(nodes)):
        ang = -_m.pi / 2 + i * 2 * _m.pi / len(nodes)
        px, py = cx + rx * _m.cos(ang), cy + ry * _m.sin(ang)
        pts.append((px, py))
    for px, py in pts:
        add_line(sl, cx, cy, px, py, t["line"], 1.25)
    cd = 1.7
    add_oval(sl, cx - cd / 2, cy - cd / 2, cd, t["accent"])
    add_text(sl, cx - cd / 2 + 0.12, cy - 0.34, cd - 0.24, 0.7, center, 15, True, "FFFFFF",
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    for (px, py), node in zip(pts, nodes):
        x, y = px - nw / 2, py - nh / 2
        add_rect(sl, x, y, nw, nh, t["soft"], line=t["line"], radius=0.12)
        name = str(node.get("name") or "")[:16]
        add_text(sl, x + 0.14, y + 0.12, nw - 0.28, 0.3, name, 12.5, True, t["ink"])
        note = str(node.get("note") or "")[:26]
        if note:
            add_text(sl, x + 0.14, y + 0.46, nw - 0.28, 0.3, note, 10, False, t["muted"])
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


def slide_stat(prs, s, ctx):
    """大数字强调页：一个关键结论 + 巨型数字（V1.7.1 新增）"""
    t = ctx["theme"]
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(sl, t)
    add_rect(sl, 0, 0, 0.2, SH, t["accent"])
    add_text(sl, M, 1.2, CW, 0.4, str(s.get("title") or "关键成果"), 20, True, t["muted"], align=PP_ALIGN.CENTER)
    value = str(s.get("value") or "—")[:22]
    add_text(sl, M, 2.05, CW, 1.9, value, 66, True, t["accent"], align=PP_ALIGN.CENTER, font=FONT_EN)
    add_rect(sl, SW / 2 - 0.55, 4.15, 1.1, 0.06, t["accent2"])
    label = str(s.get("label") or "")[:40]
    if label:
        add_text(sl, M, 4.45, CW, 0.5, label, 18, True, t["ink"], align=PP_ALIGN.CENTER)
    note = str(s.get("note") or s.get("subtitle") or "")[:80]
    if note:
        add_text(sl, M + 1.4, 5.15, CW - 2.8, 0.8, note, 12, False, t["muted"], align=PP_ALIGN.CENTER, line_spacing=1.35)
    _footer(sl, ctx, ctx["page"])
    _notes(sl, s)


RENDERERS = {
    "cover": slide_cover,
    "section": slide_section,
    "bullets": slide_bullets,
    "kpi": slide_kpi,
    "chart": slide_chart,
    "process": slide_process,
    "compare": slide_compare,
    "table": slide_table,
    "timeline": slide_timeline,
    "layers": slide_layers,
    "matrix": slide_matrix,
    "hub": slide_hub,
    "stat": slide_stat,
    "quote": slide_quote,
    "end": slide_end,
}


# ---------------- 规格校验 ----------------
def _as_list(v, limit=12):
    return [x for x in (v if isinstance(v, list) else []) if str(x).strip()][:limit]


def normalize_spec(spec: dict, expert: str = "general") -> dict:
    """把模型输出规范化为可渲染结构；缺字段补默认，不抛异常"""
    spec = spec if isinstance(spec, dict) else {}
    theme_key = str(spec.get("theme") or "").strip().lower()
    # 专家优先：政务/技术/标书/数据/产品 各有固定视觉口径；仅「通用」专家才采用模型按内容选的主题
    if expert and expert != "general":
        theme_name = EXPERT_THEME.get(expert, "business")
    else:
        theme_name = theme_key if theme_key in THEMES else "business"
    slides_in = spec.get("slides") if isinstance(spec.get("slides"), list) else []
    out = []
    section_no = 0
    for raw in slides_in[:20]:
        if not isinstance(raw, dict):
            continue
        stype = str(raw.get("type") or "").strip().lower()
        if not stype:
            stype = "bullets" if (raw.get("bullets") or raw.get("title")) else "bullets"
        s = {"type": stype, "title": str(raw.get("title") or "").strip()[:120],
             "subtitle": str(raw.get("subtitle") or "").strip()[:160],
             "kicker": str(raw.get("kicker") or "").strip()[:30],
             "notes": str(raw.get("notes") or "").strip()[:1500]}
        if stype == "section":
            section_no += 1
            s["index"] = raw.get("index") or section_no
        if stype in ("bullets", "section", "compare"):
            s["bullets"] = _as_list(raw.get("bullets"), 10)
        if stype == "kpi":
            s["items"] = [x for x in (raw.get("items") or []) if isinstance(x, dict)][:4]
            s["insight"] = str(raw.get("insight") or "").strip()[:160]
        if stype == "chart":
            s["categories"] = _as_list(raw.get("categories"), 12)
            s["series"] = [x for x in (raw.get("series") or []) if isinstance(x, dict)][:4]
            s["chart_type"] = str(raw.get("chart_type") or "bar").lower()
            s["insight"] = str(raw.get("insight") or "").strip()[:160]
        if stype == "process":
            s["steps"] = _as_list(raw.get("steps"), 6)
        if stype == "compare":
            for side in ("left", "right"):
                pane = raw.get(side) if isinstance(raw.get(side), dict) else {}
                s[side] = {"title": str(pane.get("title") or "")[:40],
                           "items": _as_list(pane.get("items"), 6)}
        if stype == "table":
            s["columns"] = _as_list(raw.get("columns"), 6)
            s["rows"] = [r for r in (raw.get("rows") or []) if isinstance(r, list)][:8]
        if stype == "timeline":
            s["events"] = [x for x in (raw.get("events") or []) if isinstance(x, dict)][:6]
        if stype == "layers":
            s["layers"] = [x for x in (raw.get("layers") or []) if isinstance(x, dict)][:5]
        if stype == "matrix":
            s["quadrants"] = [x for x in (raw.get("quadrants") or []) if isinstance(x, dict)][:4]
        if stype == "hub":
            s["nodes"] = [x for x in (raw.get("nodes") or []) if isinstance(x, dict)][:6]
            s["center"] = str(raw.get("center") or "")[:16]
        if stype == "stat":
            s["value"] = str(raw.get("value") or "")[:22]
            s["label"] = str(raw.get("label") or "")[:40]
            s["note"] = str(raw.get("note") or "")[:80]
        if stype == "quote":
            s["text"] = str(raw.get("text") or "").strip()[:400]
            s["source"] = str(raw.get("source") or "").strip()[:80]
        if stype == "end":
            s["contact"] = str(raw.get("contact") or "").strip()[:80]
        out.append(s)
    if not out:
        out = [{"type": "bullets", "title": "要点", "bullets": ["（模型未返回可渲染内容）"], "subtitle": "", "kicker": "", "notes": ""}]
    return {
        "title": str(spec.get("title") or "演示文稿").strip()[:80],
        "subtitle": str(spec.get("subtitle") or "").strip()[:160],
        "meta": str(spec.get("meta") or "").strip()[:80],
        "kicker": str(spec.get("kicker") or "").strip()[:30],
        "theme": theme_name,
        "slides": out,
    }


def build_deck(spec: dict, path: str, expert: str = "general") -> dict:
    """渲染并保存 PPTX，返回统计信息"""
    norm = normalize_spec(spec, expert)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SW), Inches(SH)
    ctx = {
        "theme": THEMES[norm["theme"]],
        "deck_title": norm["title"],
        "meta": norm["meta"] or datetime.now().strftime("%Y 年 %m 月"),
        "kicker": norm.get("kicker") or "汇报材料",
        "total": 0,
        "page": 0,
    }
    # 若首屏不是封面，自动补一张封面
    slides = norm["slides"]
    if not slides or slides[0].get("type") != "cover":
        slides = [{"type": "cover", "title": norm["title"], "subtitle": norm["subtitle"],
                   "meta": norm["meta"], "notes": ""}] + slides
    ctx["total"] = len(slides)
    page = 0
    for s in slides:
        stype = s.get("type") if s.get("type") in RENDERERS else "bullets"
        if stype != "cover":
            page += 1
            ctx["page"] = page
        RENDERERS[stype](prs, s, ctx)
    prs.save(path)
    return {"slides": len(slides), "theme": norm["theme"],
            "layouts": [str(x.get("type") or "bullets") for x in slides]}


def deck_from_markdown(text: str, title: str = "", expert: str = "general") -> dict:
    """兜底：模型没给出合法 JSON 时，把 Markdown 标题层级转成要点页"""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    slides, cur = [], None
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.+)$", ln.strip())
        if m:
            level = len(m.group(1))
            if level == 1:
                if not title:
                    title = m.group(2).strip()
                continue
            cur = {"type": "bullets", "title": m.group(2).strip(), "bullets": [], "subtitle": "", "kicker": "", "notes": ""}
            slides.append(cur)
        elif cur is not None:
            t = re.sub(r"^[-*•\d.\s]+", "", ln.strip())
            if t:
                cur["bullets"].append(t[:120])
    if not slides:
        plain = [x.strip() for x in re.split(r"[。\n]", text or "") if x.strip()][:6]
        slides = [{"type": "bullets", "title": title or "要点", "bullets": plain}]
    for s in slides:
        s["bullets"] = s["bullets"][:8]
    return {"title": title or "演示文稿", "subtitle": "", "theme": EXPERT_THEME.get(expert, "business"), "slides": slides}