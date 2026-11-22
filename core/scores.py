"""经典风险模型评分：Altman Z-score、Piotroski F-score、五维评分与雷达图。

所有数值由确定性公式计算，LLM 不参与。数据缺失的检查项按“不参与计分”处理，
不猜测、不补值。
"""
import plotly.graph_objects as go

from .evaluator import eval_text

DIMENSIONS = ["偿债能力", "盈利能力", "营运能力", "成长能力", "现金流质量"]


def _fmt(x, pct=False) -> str:
    if x is None:
        return "缺失"
    return f"{x:.1%}" if pct else f"{x:.2f}"


def grade(total: float) -> str:
    if total >= 80:
        return "优秀"
    if total >= 60:
        return "良好"
    if total >= 40:
        return "一般"
    return "较差"


# ---------------- Altman Z-score ----------------

def altman_z(data: dict, market_value: float = None) -> dict:
    """上市制造业用 Z（X4=股权市值/总负债），否则用非上市 Z'（X4=账面净资产/总负债）。"""
    x1 = eval_text(data, "(流动资产合计 - 流动负债合计) / 资产总计")
    x2 = eval_text(data, "(nz(盈余公积) + nz(未分配利润)) / 资产总计")
    x3 = eval_text(data, "(nz(利润总额) + nz(利息费用)) / 资产总计")
    debt = data.get("负债合计")
    if market_value and debt:
        x4, listed = market_value / debt, True
    else:
        x4, listed = eval_text(data, "所有者权益合计 / 负债合计"), False
    x5 = eval_text(data, "营业收入 / 资产总计")

    parts = {"X1": x1, "X2": x2, "X3": x3, "X4": x4, "X5": x5}
    if any(v is None for v in parts.values()):
        return {"z": None, "zone": "数据不足", "model": "上市" if listed else "非上市", "parts": parts}

    if listed:
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        zone = "安全" if z > 2.99 else ("灰色" if z >= 1.81 else "危险")
        model = "上市制造业 Z"
    else:
        z = 0.717 * x1 + 0.847 * x2 + 3.107 * x3 + 0.420 * x4 + 0.998 * x5
        zone = "安全" if z > 2.90 else ("灰色" if z >= 1.23 else "危险")
        model = "非上市 Z'"
    return {"z": z, "zone": zone, "model": model, "parts": parts}


# ---------------- Piotroski F-score ----------------

def _trio(avail: bool, ok_fn) -> object:
    """三态判定：数据缺失返回 None，否则返回 bool(ok_fn())。ok_fn 惰性求值避免 None 运算。"""
    return None if not avail else bool(ok_fn())


def f_score(data: dict) -> dict:
    """9 项二元检查，每项 1 分；数据缺失的检查项不参与计分（不猜值）。"""
    roa = eval_text(data, "净利润 / 资产总计_平均")
    roa_prev = eval_text(data, "净利润_上期 / 资产总计_上期")
    cfo = data.get("经营活动产生的现金流量净额")
    cfo_ratio = eval_text(data, "经营活动产生的现金流量净额 / 资产总计_平均")
    lev = eval_text(data, "非流动负债合计 / 资产总计")
    lev_prev = eval_text(data, "非流动负债合计_上期 / 资产总计_上期")
    cr = eval_text(data, "流动资产合计 / 流动负债合计")
    cr_prev = eval_text(data, "流动资产合计_上期 / 流动负债合计_上期")
    gm = eval_text(data, "(营业收入 - 营业成本) / 营业收入")
    gm_prev = eval_text(data, "(营业收入_上期 - 营业成本_上期) / 营业收入_上期")
    at = eval_text(data, "营业收入 / 资产总计_平均")
    at_prev = eval_text(data, "营业收入_上期 / 资产总计_上期")
    shares, shares_prev = data.get("股本"), data.get("股本_上期")

    checks = [
        ("ROA > 0", _trio(roa is not None, lambda: roa > 0), _fmt(roa, True)),
        ("经营现金流 > 0", _trio(cfo is not None, lambda: cfo > 0),
         "缺失" if cfo is None else f"{cfo / 1e8:.2f} 亿元"),
        ("ROA 同比提升",
         _trio(roa is not None and roa_prev is not None, lambda: roa > roa_prev),
         f"{_fmt(roa_prev, True)} → {_fmt(roa, True)}"),
        ("应计质量（现金流 > ROA）",
         _trio(cfo_ratio is not None and roa is not None, lambda: cfo_ratio > roa),
         f"现金流/资产 {_fmt(cfo_ratio, True)} vs ROA {_fmt(roa, True)}"),
        ("长期负债率下降",
         _trio(lev is not None and lev_prev is not None, lambda: lev < lev_prev),
         f"{_fmt(lev_prev, True)} → {_fmt(lev, True)}"),
        ("流动比率提升",
         _trio(cr is not None and cr_prev is not None, lambda: cr > cr_prev),
         f"{_fmt(cr_prev)} → {_fmt(cr)}"),
        ("未增发股本",
         _trio(shares is not None and shares_prev is not None,
               lambda: abs(shares - shares_prev) < 1e-6),
         "缺失" if shares is None else f"{shares / 1e8:.2f} 亿股"),
        ("毛利率提升",
         _trio(gm is not None and gm_prev is not None, lambda: gm > gm_prev),
         f"{_fmt(gm_prev, True)} → {_fmt(gm, True)}"),
        ("总资产周转率提升",
         _trio(at is not None and at_prev is not None, lambda: at > at_prev),
         f"{_fmt(at_prev)} → {_fmt(at)}"),
    ]
    rows, passed, total = [], 0, 0
    for name, ok, detail in checks:
        if ok is None:
            rows.append({"项目": name, "通过": None, "说明": "数据缺失"})
        else:
            total += 1
            passed += int(ok)
            rows.append({"项目": name, "通过": bool(ok), "说明": detail})
    return {"score": passed, "denominator": total, "rows": rows}


# ---------------- 五维评分 ----------------

_CHECKS = {
    "偿债能力": [
        ("资产负债率 ≤ 50%", "负债合计 / 资产总计", lambda v: v <= 0.5, True),
        ("流动比率 ≥ 1.2", "流动资产合计 / 流动负债合计", lambda v: v >= 1.2, False),
        ("速动比率 ≥ 0.8", "(流动资产合计 - 存货) / 流动负债合计", lambda v: v >= 0.8, False),
        ("利息保障倍数 ≥ 3", "(利润总额 + abs(nz(利息费用))) / abs(nz(利息费用))",
         lambda v: v >= 3, False),
    ],
    "盈利能力": [
        ("ROE ≥ 8%", "净利润 / 所有者权益合计_平均", lambda v: v >= 0.08, True),
        ("净利率 ≥ 3%", "净利润 / 营业收入", lambda v: v >= 0.03, True),
        ("毛利率 ≥ 20%", "(营业收入 - 营业成本) / 营业收入", lambda v: v >= 0.2, True),
        ("ROA ≥ 3%", "净利润 / 资产总计_平均", lambda v: v >= 0.03, True),
    ],
    "营运能力": [
        ("应收账款周转率 ≥ 4", "营业收入 / 应收账款_平均", lambda v: v >= 4, False),
        ("存货周转率 ≥ 4", "营业成本 / 存货_平均", lambda v: v >= 4, False),
        ("总资产周转率 ≥ 0.6", "营业收入 / 资产总计_平均", lambda v: v >= 0.6, False),
    ],
    "成长能力": [
        ("营收同比 > 0", "yoy(营业收入, 营业收入_上期)", lambda v: v > 0, True),
        ("净利润同比 > 0", "yoy(净利润, 净利润_上期)", lambda v: v > 0, True),
        ("总资产同比 > 0", "yoy(资产总计, 资产总计_上期)", lambda v: v > 0, True),
    ],
    "现金流质量": [
        ("经营现金流 > 0", "经营活动产生的现金流量净额", lambda v: v > 0, False),
        ("净现比 ≥ 0.8", "经营活动产生的现金流量净额 / 净利润", lambda v: v >= 0.8, False),
        ("销售收现比率 ≥ 0.9",
         "销售商品提供劳务收到的现金 / 营业收入", lambda v: v >= 0.9, True),
        ("现金流量比率 ≥ 0.1",
         "经营活动产生的现金流量净额 / 流动负债合计", lambda v: v >= 0.1, False),
    ],
}


def five_dimension_scores(data: dict):
    """每个维度 20 分，按通过项占比折算；缺失项不参与该维度计分。"""
    scores, detail = {}, []
    for dim, checks in _CHECKS.items():
        passed = total = 0
        for name, formula, predicate, is_pct in checks:
            value = eval_text(data, formula)
            if value is None:
                detail.append({"维度": dim, "检查项": name, "结果": "数据缺失", "通过": False})
                continue
            ok = bool(predicate(value))
            total += 1
            passed += ok
            detail.append({
                "维度": dim, "检查项": name,
                "结果": _fmt(value, is_pct), "通过": ok,
            })
        scores[dim] = round(20.0 * passed / total, 1) if total else 0.0
    return scores, detail


def radar_chart(scores) -> go.Figure:
    """单公司传 {维度: 分}，多公司传 {公司名: {维度: 分}}。"""
    multi = bool(scores) and isinstance(next(iter(scores.values())), dict)
    groups = scores if multi else ({"本公司": scores} if scores else {})
    fig = go.Figure()
    for name, dims in groups.items():
        vals = [float(dims.get(d, 0) or 0) for d in DIMENSIONS]
        fig.add_trace(go.Scatterpolar(
            r=vals + vals[:1], theta=DIMENSIONS + DIMENSIONS[:1],
            fill="toself", name=name,
        ))
    fig.update_layout(
        polar={"radialaxis": {"range": [0, 20], "visible": True}},
        height=420, margin={"l": 40, "r": 40, "t": 40, "b": 40},
        showlegend=multi,
    )
    return fig
