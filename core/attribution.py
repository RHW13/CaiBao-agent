"""归因分解：杜邦三因素连环替代、毛利率变动瀑布、管理层讨论佐证检索。"""
import plotly.graph_objects as go

from .evaluator import eval_text
from .notes_scan import build_notes
from .ratios import build_data

MDNA_KEYWORDS = ["营业收入", "毛利率", "净利润", "现金流", "应收账款", "存货", "研发", "风险"]


def dupont_attribution(pdf_data: dict) -> dict:
    """连环替代法：ROE 变动 = 净利率贡献 + 周转率贡献 + 权益乘数贡献。"""
    data = build_data(pdf_data)
    m0 = eval_text(data, "净利润_上期 / 营业收入_上期")
    t0 = eval_text(data, "营业收入_上期 / 资产总计_上期")
    l0 = eval_text(data, "资产总计_上期 / 所有者权益合计_上期")
    m1 = eval_text(data, "净利润 / 营业收入")
    t1 = eval_text(data, "营业收入 / 资产总计_平均")
    l1 = eval_text(data, "资产总计_平均 / 所有者权益合计_平均")

    if any(v is None for v in (m0, t0, l0, m1, t1, l1)):
        return {"ok": False, "rows": [], "roe_prev": None, "roe_cur": None, "change": None}

    base = m0 * t0 * l0
    step1 = m1 * t0 * l0
    step2 = m1 * t1 * l0
    step3 = m1 * t1 * l1
    rows = [
        {"因素": "销售净利率", "上期": m0, "本期": m1, "贡献": step1 - base},
        {"因素": "总资产周转率", "上期": t0, "本期": t1, "贡献": step2 - step1},
        {"因素": "权益乘数", "上期": l0, "本期": l1, "贡献": step3 - step2},
    ]
    return {
        "ok": True, "rows": rows,
        "roe_prev": base, "roe_cur": step3, "change": step3 - base,
        "identity_ok": abs(sum(r["贡献"] for r in rows) - (step3 - base)) < 1e-12,
    }


def gross_margin_breakdown(pdf_data: dict) -> dict:
    """毛利率变动 = 收入影响（成本不变）+ 成本影响（收入已变）。"""
    data = build_data(pdf_data)
    r0, c0 = data.get("营业收入_上期"), data.get("营业成本_上期")
    r1, c1 = data.get("营业收入"), data.get("营业成本")
    if None in (r0, c0, r1, c1) or r0 == 0 or r1 == 0:
        return {"ok": False, "rows": [], "figure": None}

    gm0 = 1 - c0 / r0
    gm1 = 1 - c1 / r1
    revenue_effect = c0 / r0 - c0 / r1
    cost_effect = (c0 - c1) / r1
    rows = [
        {"项目": "上期毛利率", "影响": gm0},
        {"项目": "收入影响", "影响": revenue_effect},
        {"项目": "成本影响", "影响": cost_effect},
        {"项目": "本期毛利率", "影响": gm1},
    ]

    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute", "relative", "relative", "total"],
        x=[r["项目"] for r in rows],
        y=[gm0 * 100, revenue_effect * 100, cost_effect * 100, gm1 * 100],
        text=[f"{gm0:.1%}", f"{revenue_effect:+.1%}", f"{cost_effect:+.1%}", f"{gm1:.1%}"],
        textposition="outside",
        connector={"line": {"color": "#9aa4b2"}},
    ))
    fig.update_layout(
        title="毛利率变动瀑布（百分点）", height=380,
        margin={"l": 40, "r": 30, "t": 60, "b": 40}, showlegend=False,
    )
    return {"ok": True, "rows": rows, "gm_prev": gm0, "gm_cur": gm1,
            "change": gm1 - gm0, "figure": fig}


def mdna_evidence(pdf_data: dict, keywords: list = None, limit: int = 4) -> list:
    """在“管理层讨论与分析”章节检索佐证原文，标注为公司解释。"""
    notes = build_notes(pdf_data.get("pages") or [])
    starts = [p for p in (pdf_data.get("stmt_starts") or {}).values() if p]
    upper = min(starts) if starts else 10 ** 9
    hits = notes.search(keywords or MDNA_KEYWORDS, sections=["管理层讨论与分析"],
                        limit=limit * 2, pad=180)
    hits = [h for h in hits if 5 < h["page"] < upper][:limit]
    if not hits:
        hits = [h for h in notes.search(keywords or MDNA_KEYWORDS, limit=limit * 2, pad=180)
                if 5 < h["page"] < upper][:limit]
    return [{"page": h["page"], "snippet": h["snippet"], "score": h["score"]} for h in hits]
