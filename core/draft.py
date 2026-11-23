"""投研底稿：把已算好的分析汇编为结构化底稿，并导出 Word / PDF。

原则：本模块不重新计算任何指标，只调用 ratios / scores / risk / trend /
attribution / forecast 的既有结果；LLM 仅用于撰写结论文字，且结论中的数字
必须能在底稿数值集合中回查到，否则该条结论被丢弃。
"""
import io
import re
from datetime import date
from xml.sax.saxutils import escape

import pandas as pd

from . import config
from .attribution import dupont_attribution, gross_margin_breakdown
from .benchmark import compare
from .forecast import dcf, forecast
from .ratios import build_data, compute_ratios
from .risk import scan_risks
from .scores import altman_z, f_score, five_dimension_scores, grade
from .trend import trend_panel

CHAPTERS = ["公司概览", "财务体检", "风险清单", "拐点信号", "结论", "跟踪清单"]

_SYS_PROMPT = (
    "你是投研底稿撰写助手。只能使用给定事实中的数据，不得引入外部知识，不得自行计算新数字。\n"
    "输出 3-5 条结论，每条一行，以「- 」开头，每条必须包含至少一个事实中的数字。\n"
    "不得使用「可能」「大概」等模糊表述，不得给出买卖建议。"
)


def _pct(v):
    return "—" if v is None else f"{v:.2%}"


def _amt(v):
    return "—" if v is None else f"{v / 1e8:.2f} 亿元"


def assemble(data: dict, results: dict = None, extra: dict = None, index=None,
             use_llm: bool = True) -> dict:
    """汇编底稿。results 为多公司对比（{公司名: pdf_data}，≥2 家时生成对标章节）。"""
    meta = data.get("meta", {})
    data_map = build_data(data, extra)
    ratios = compute_ratios(data, extra=extra)
    dims, dim_detail = five_dimension_scores(data_map)
    total = round(sum(dims.values()), 1)
    panel = trend_panel(data)
    risks = scan_risks(data, index=index, use_llm=False)["signals"]
    dupont = dupont_attribution(data)
    margin = gross_margin_breakdown(data)
    fc = forecast(data)
    val = dcf(data)

    bench = None
    if results and len(results) >= 2:
        detail, summary = compare(results)
        bench = {"明细": detail, "汇总": summary}

    draft = {
        "公司概览": {
            "公司": meta.get("company") or "未识别公司",
            "报告期": f"{meta.get('year') or ''} {meta.get('period') or '年度报告'}".strip(),
            "五维评分": f"{total:.1f} / 100（{grade(total)}）",
            "预警等级": f"{panel['level']}（{panel['score']} 分）",
            "生成日期": date.today().isoformat(),
        },
        "财务体检": {
            "比率": ratios,
            "五维": dims,
            "五维明细": dim_detail,
            "总分": total,
            "等级": grade(total),
            "杜邦": dupont,
            "毛利率": margin,
            "AltmanZ": altman_z(data_map),
            "F分数": f_score(data_map),
        },
        "风险清单": risks,
        "拐点信号": panel,
        "对标": bench,
        "估值": {"预测": fc["rows"], "DCF": val},
        "结论": [],
        "跟踪清单": [],
    }
    draft["结论"] = conclusion_bullets(draft, use_llm=use_llm)
    draft["跟踪清单"] = _tracking(draft, data_map)
    return draft


def _facts(draft: dict) -> str:
    """把底稿中的关键数字压成一段文字，作为结论生成的唯一事实来源。"""
    overview = draft["公司概览"]
    health = draft["财务体检"]
    panel = draft["拐点信号"]
    risks = draft["风险清单"]
    lines = [
        f"公司：{overview['公司']}；报告期：{overview['报告期']}。",
        f"五维评分 {health['总分']:.1f} 分，等级 {health['等级']}；"
        + "；".join(f"{k} {v:.1f}" for k, v in health["五维"].items()) + "。",
        f"拐点预警 {panel['level']}，预警分 {panel['score']}，"
        f"触发信号 {sum(1 for s in panel['signals'] if s['触发'])} 条。",
        f"风险信号共 {len(risks)} 条，其中高风险 {sum(1 for r in risks if r['等级'] == '高危')} 条。",
    ]
    ratio_rows = health["比率"]
    for name in ("净资产收益率(ROE)", "销售毛利率", "销售净利率", "资产负债率", "流动比率"):
        row = ratio_rows[ratio_rows["指标"] == name]
        if row.empty or pd.isna(row.iloc[0]["数值"]):
            continue
        lines.append(f"{name} {row.iloc[0]['数值']:.4f}{row.iloc[0]['单位']}。")
    val = draft["估值"]["DCF"]
    if val.get("ok"):
        lines.append(f"DCF 企业价值 {val['enterprise'] / 1e8:.2f} 亿元。")
    if draft["对标"]:
        lines.append(f"对标样本 {draft['对标']['明细'].shape[1]} 家公司。")
    return "\n".join(lines)


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set:
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUM.finditer(text)}


def _rule_bullets(draft: dict) -> list:
    health, panel, risks = draft["财务体检"], draft["拐点信号"], draft["风险清单"]
    bullets = []
    low_dim = min(health["五维"], key=health["五维"].get) if health["五维"] else None
    bullets.append(
        f"五维综合评分 {health['总分']:.1f} 分（{health['等级']}），"
        + (f"其中 {low_dim} 维度得分最低（{health['五维'][low_dim]:.1f} 分）。" if low_dim else "。")
    )
    fired = [s for s in panel["signals"] if s["触发"]]
    bullets.append(
        f"拐点预警等级 {panel['level']}（{panel['score']} 分），触发 {len(fired)} 条背离信号"
        + ("：" + "；".join(s["名称"] for s in fired) if fired else "。")
    )
    high = [r for r in risks if r["等级"] == "高危"]
    bullets.append(
        f"共识别 {len(risks)} 条风险信号，其中高风险 {len(high)} 条"
        + ("：" + "、".join(r["类型"] for r in high) if high else "。")
    )
    dupont = health["杜邦"]
    if dupont.get("ok"):
        top = max(dupont["rows"], key=lambda r: abs(r["贡献"]))
        bullets.append(
            f"ROE 由 {dupont['roe_prev']:.2%} 变为 {dupont['roe_cur']:.2%}，"
            f"主要驱动因素为{top['因素']}（贡献 {top['贡献']:+.2%}）。"
        )
    val = draft["估值"]["DCF"]
    if val.get("ok"):
        bullets.append(f"两阶段 DCF 测算企业价值 {val['enterprise'] / 1e8:.2f} 亿元。")
    return bullets


def conclusion_bullets(draft: dict, use_llm: bool = True) -> list:
    """生成结论。LLM 可用时撰写文字，并按「数字回查」过滤编造数字的条目。"""
    facts = _facts(draft)
    if use_llm:
        client = config.get_llm_client()
        if client is not None:
            try:
                resp = client.chat.completions.create(
                    model=config.get_llm_model(),
                    messages=[
                        {"role": "system", "content": _SYS_PROMPT},
                        {"role": "user", "content": f"事实：\n{facts}\n\n请输出结论。"},
                    ],
                    temperature=0,
                )
                text = (resp.choices[0].message.content or "").strip()
                bullets = [ln.strip("-• ").strip() for ln in text.splitlines() if ln.strip()]
                allowed = _numbers(facts)
                kept = [b for b in bullets if b and _numbers(b) <= allowed]
                if kept:
                    return kept[:5]
            except Exception:
                pass
    return _rule_bullets(draft)


def _tracking(draft: dict, data_map: dict) -> list:
    panel, risks = draft["拐点信号"], draft["风险清单"]
    items = [f"持续跟踪 {t}" for t in panel["track"]]
    for r in risks:
        if r["等级"] == "高危":
            items.append(f"核实「{r['类型']}」：{r['依据'][:60]}")
    for name in ("应收账款", "存货", "合同负债", "经营活动产生的现金流量净额"):
        if data_map.get(name) is None:
            items.append(f"补充披露缺失科目：{name}")
    return list(dict.fromkeys(items))


# ---------------- 导出：Markdown ----------------

def _md_table(df: pd.DataFrame, digits: int = 2) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if isinstance(v, float) and pd.notna(v):
                cells.append(f"{v:,.{digits}f}")
            else:
                cells.append("—" if v is None or pd.isna(v) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def to_markdown(draft: dict, chapters: list = None) -> str:
    picked = chapters or CHAPTERS
    out = [f"# {draft['公司概览']['公司']} 投研底稿", "", f"> 报告期：{draft['公司概览']['报告期']}　"
           f"生成日期：{draft['公司概览']['生成日期']}", ""]
    for ch in picked:
        out.append(f"## {ch}")
        if ch == "公司概览":
            out += [f"- {k}：{v}" for k, v in draft['公司概览'].items()]
        elif ch == "财务体检":
            health = draft["财务体检"]
            out.append(f"五维评分 **{health['总分']:.1f} / 100（{health['等级']}）**")
            out.append(_md_table(pd.DataFrame(
                [{"维度": k, "得分": v} for k, v in health["五维"].items()])))
            out.append("")
            out.append(_md_table(health["比率"][["指标", "数值", "单位", "备注"]]))
        elif ch == "风险清单":
            if draft["风险清单"]:
                out.append(_md_table(pd.DataFrame(draft["风险清单"])[["类型", "等级", "依据"]]))
            else:
                out.append("本次扫描未发现规则命中的风险信号。")
        elif ch == "拐点信号":
            panel = draft["拐点信号"]
            out.append(f"预警等级 **{panel['level']}**（{panel['score']} 分）")
            out.append("")
            out.append(_md_table(pd.DataFrame(
                [{"信号": s["名称"], "触发": "是" if s["触发"] else "否", "说明": s["说明"]}
                 for s in panel["signals"]])))
        elif ch == "结论":
            out += [f"- {b}" for b in draft["结论"]]
        elif ch == "跟踪清单":
            out += [f"- {t}" for t in draft["跟踪清单"]] or ["- 无"]
        out.append("")
    return "\n".join(out)


# ---------------- 导出：Word ----------------

def _docx_table(doc, df: pd.DataFrame):
    from docx.shared import Pt

    table = doc.add_table(rows=1, cols=len(df.columns))
    table.style = "Table Grid"
    for i, col in enumerate(df.columns):
        cell = table.rows[0].cells[i]
        cell.text = str(col)
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.size = Pt(9)
    for _, row in df.iterrows():
        cells = table.add_row().cells
        for i, v in enumerate(row):
            if isinstance(v, float) and pd.notna(v):
                cells[i].text = f"{v:,.2f}"
            else:
                cells[i].text = "—" if v is None or pd.isna(v) else str(v)
            for run in cells[i].paragraphs[0].runs:
                run.font.size = Pt(9)


def to_docx(draft: dict, chapters: list = None) -> bytes:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    picked = chapters or CHAPTERS
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    doc.add_heading(f"{draft['公司概览']['公司']} 投研底稿", level=0)
    doc.add_paragraph(f"报告期：{draft['公司概览']['报告期']}　生成日期：{draft['公司概览']['生成日期']}")

    for ch in picked:
        doc.add_heading(ch, level=1)
        if ch == "公司概览":
            for k, v in draft["公司概览"].items():
                doc.add_paragraph(f"{k}：{v}", style="List Bullet")
        elif ch == "财务体检":
            health = draft["财务体检"]
            doc.add_paragraph(f"五维评分：{health['总分']:.1f} / 100（{health['等级']}）")
            _docx_table(doc, pd.DataFrame(
                [{"维度": k, "得分": v} for k, v in health["五维"].items()]))
            doc.add_paragraph("")
            _docx_table(doc, health["比率"][["指标", "数值", "单位", "备注"]])
        elif ch == "风险清单":
            if draft["风险清单"]:
                _docx_table(doc, pd.DataFrame(draft["风险清单"])[["类型", "等级", "依据"]])
            else:
                doc.add_paragraph("本次扫描未发现规则命中的风险信号。")
        elif ch == "拐点信号":
            panel = draft["拐点信号"]
            doc.add_paragraph(f"预警等级：{panel['level']}（{panel['score']} 分）")
            _docx_table(doc, pd.DataFrame(
                [{"信号": s["名称"], "触发": "是" if s["触发"] else "否", "说明": s["说明"]}
                 for s in panel["signals"]]))
        elif ch == "结论":
            for b in draft["结论"]:
                doc.add_paragraph(b, style="List Bullet")
        elif ch == "跟踪清单":
            for t in draft["跟踪清单"] or ["无"]:
                doc.add_paragraph(t, style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------- 导出：PDF ----------------

def to_pdf(draft: dict, chapters: list = None) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                        TableStyle)
    except ImportError as exc:  # pragma: no cover - 依赖缺失时明确报错
        raise RuntimeError("未安装 reportlab，无法导出 PDF，请改用 Word 导出。") from exc

    font = "STSong-Light"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["Normal"], fontName=font,
                          fontSize=9.5, leading=14, alignment=TA_LEFT)
    h1 = ParagraphStyle("h1", parent=body, fontSize=16, leading=22, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=body, fontSize=12, leading=18, spaceBefore=10,
                        spaceAfter=4)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=10)

    def tbl(df: pd.DataFrame, widths=None):
        data = [[Paragraph(escape(str(c)), cell) for c in df.columns]]
        for _, row in df.iterrows():
            data.append([
                Paragraph(escape(f"{v:,.2f}" if isinstance(v, float) and pd.notna(v)
                                 else ("—" if v is None or pd.isna(v) else str(v))), cell)
                for v in row
            ])
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return table

    picked = chapters or CHAPTERS
    story = [Paragraph(escape(f"{draft['公司概览']['公司']} 投研底稿"), h1),
             Paragraph(escape(f"报告期：{draft['公司概览']['报告期']}　"
                              f"生成日期：{draft['公司概览']['生成日期']}"), body),
             Spacer(1, 6)]

    for ch in picked:
        story.append(Paragraph(escape(ch), h2))
        if ch == "公司概览":
            for k, v in draft["公司概览"].items():
                story.append(Paragraph(escape(f"· {k}：{v}"), body))
        elif ch == "财务体检":
            health = draft["财务体检"]
            story.append(Paragraph(
                escape(f"五维评分：{health['总分']:.1f} / 100（{health['等级']}）"), body))
            story.append(tbl(pd.DataFrame(
                [{"维度": k, "得分": v} for k, v in health["五维"].items()]),
                [4 * cm, 4 * cm]))
            story.append(Spacer(1, 6))
            story.append(tbl(health["比率"][["指标", "数值", "单位", "备注"]],
                             [6 * cm, 3 * cm, 2 * cm, 6 * cm]))
        elif ch == "风险清单":
            if draft["风险清单"]:
                story.append(tbl(pd.DataFrame(draft["风险清单"])[["类型", "等级", "依据"]],
                                 [3.5 * cm, 1.8 * cm, 11.7 * cm]))
            else:
                story.append(Paragraph("本次扫描未发现规则命中的风险信号。", body))
        elif ch == "拐点信号":
            panel = draft["拐点信号"]
            story.append(Paragraph(
                escape(f"预警等级：{panel['level']}（{panel['score']} 分）"), body))
            story.append(tbl(pd.DataFrame(
                [{"信号": s["名称"], "触发": "是" if s["触发"] else "否", "说明": s["说明"]}
                 for s in panel["signals"]]), [3.5 * cm, 1.5 * cm, 12 * cm]))
        elif ch == "结论":
            for b in draft["结论"]:
                story.append(Paragraph(escape(f"· {b}"), body))
        elif ch == "跟踪清单":
            for t in draft["跟踪清单"] or ["无"]:
                story.append(Paragraph(escape(f"· {t}"), body))
        story.append(Spacer(1, 8))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                            title=f"{draft['公司概览']['公司']} 投研底稿")
    doc.build(story)
    return buf.getvalue()


# ---------------- 导出：表格 PDF ----------------

def _fmt_cell(v) -> str:
    """按值类型格式化单元格，与界面数值格式保持一致。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:,.4f}"
    return str(v)


def _col_weights(df: pd.DataFrame) -> list:
    """按列内容长度估算列宽权重，避免窄列浪费、宽列被挤压。"""
    weights = []
    for col in df.columns:
        texts = [str(col)] + [str(_fmt_cell(v)) for v in df[col].head(80)]
        weights.append(min(max(max(len(t) for t in texts), 4), 36))
    total = float(sum(weights)) or 1.0
    return [w / total for w in weights]


def tables_to_pdf(tables: list, title: str = "表格导出", subtitle: str = "") -> bytes:
    """把若干表格排版为一个 PDF，保留表头、网格、列结构与数值格式。

    tables：[{"name": 表名, "frame": DataFrame}]。列数较多时自动使用横向 A4，
    跨页重复表头，页脚标注文件名与页码。
    """
    if not tables:
        raise ValueError("没有可导出的表格。")
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                        Table, TableStyle)
    except ImportError as exc:  # pragma: no cover - 依赖缺失时明确报错
        raise RuntimeError("未安装 reportlab，无法导出 PDF。") from exc

    font = "STSong-Light"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    base = getSampleStyleSheet()
    body = ParagraphStyle("tbl-body", parent=base["Normal"], fontName=font,
                          fontSize=8.5, leading=12)
    h1 = ParagraphStyle("tbl-h1", parent=body, fontSize=15, leading=20, spaceAfter=4)
    h2 = ParagraphStyle("tbl-h2", parent=body, fontSize=11, leading=15,
                        spaceBefore=8, spaceAfter=4)
    cell = ParagraphStyle("tbl-cell", parent=body, fontSize=7.5, leading=10)

    max_cols = max(len(t["frame"].columns) for t in tables)
    page = landscape(A4) if max_cols > 4 else A4
    margin = 1.2 * cm
    usable = page[0] - 2 * margin

    def build_table(df: pd.DataFrame):
        widths = [usable * w for w in _col_weights(df)]
        data = [[Paragraph(escape(str(c)), cell) for c in df.columns]]
        for _, row in df.iterrows():
            data.append([Paragraph(escape(_fmt_cell(v)) or "&nbsp;", cell) for v in row])
        table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        style = [
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9C2CC")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F7F9FB")))
        table.setStyle(TableStyle(style))
        return table

    story = [Paragraph(escape(title), h1)]
    if subtitle:
        story.append(Paragraph(escape(subtitle), body))
    story.append(Spacer(1, 6))
    for t in tables:
        df = t["frame"]
        story.append(Paragraph(escape(f"{t['name']}（{len(df)} 行）"), h2))
        story.append(build_table(df))
        story.append(Spacer(1, 10))

    buf = io.BytesIO()

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(margin, 0.7 * cm, title)
        canvas.drawRightString(page[0] - margin, 0.7 * cm, f"第 {doc.page} 页")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin,
                            topMargin=1.2 * cm, bottomMargin=1.4 * cm, title=title)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
