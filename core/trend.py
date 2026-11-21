"""拐点信号：两期序列对齐 + 5 条背离规则打分（含历史回测接口）。

单份年报只含本期/上期两列，因此序列最多 2 期；回测需 ≥3 期，
不足时诚实标注“样本不足”，不编造命中率。
"""
import pandas as pd

from .evaluator import eval_text
from .ratios import build_data

# (名称, 权重, 跟踪科目)
_SIGNALS = [
    ("应收账款增速超营收", 20, "应收账款、回款政策"),
    ("存货增速超营收", 20, "存货、跌价准备"),
    ("净利润与经营现金流背离", 25, "经营活动现金流、应收账款"),
    ("毛利率下滑", 15, "毛利率、营业成本"),
    ("合同负债下降", 20, "合同负债、在手订单"),
]


def _growth(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev)


def _pct(x) -> str:
    return "—" if x is None else f"{x:.1%}"


def _amt(x) -> str:
    return "—" if x is None else f"{x / 1e8:.2f} 亿元"


def build_series(pdf_data: dict) -> pd.DataFrame:
    """单份年报 → 上期/本期两期序列。"""
    data = build_data(pdf_data)
    rows = []
    for label, suffix in [("上期", "_上期"), ("本期", "")]:
        def get(key):
            return data.get(key + suffix)

        revenue, cost = get("营业收入"), get("营业成本")
        margin = None if revenue is None or cost is None else (revenue - cost) / revenue
        rows.append({
            "报告期": label,
            "营业收入": revenue,
            "营业成本": cost,
            "净利润": get("净利润"),
            "应收账款": get("应收账款"),
            "存货": get("存货"),
            "合同负债": get("合同负债"),
            "毛利率": margin,
            "经营活动现金流量净额": get("经营活动产生的现金流量净额"),
        })
    return pd.DataFrame(rows)


def divergence_signals(data: dict) -> list:
    """5 条背离规则独立判断，返回 [{名称, 触发, 权重, 说明, 跟踪}]。"""
    rev_g = _growth(data.get("营业收入"), data.get("营业收入_上期"))
    ar_g = _growth(data.get("应收账款"), data.get("应收账款_上期"))
    inv_g = _growth(data.get("存货"), data.get("存货_上期"))
    net_profit = data.get("净利润")
    cash = data.get("经营活动产生的现金流量净额")
    gm = eval_text(data, "(营业收入 - 营业成本) / 营业收入")
    gm_prev = eval_text(data, "(营业收入_上期 - 营业成本_上期) / 营业收入_上期")
    contract = data.get("合同负债")
    contract_prev = data.get("合同负债_上期")

    fired = [
        rev_g is not None and ar_g is not None and ar_g - rev_g > 0.10,
        rev_g is not None and inv_g is not None and inv_g - rev_g > 0.10,
        net_profit is not None and cash is not None and net_profit > 0
        and (cash < 0 or cash / net_profit < 0.5),
        gm is not None and gm_prev is not None and gm_prev - gm > 0.03,
        contract is not None and contract_prev not in (None, 0)
        and (contract - contract_prev) / contract_prev < -0.20,
    ]
    details = [
        f"应收 {_pct(ar_g)} vs 营收 {_pct(rev_g)}",
        f"存货 {_pct(inv_g)} vs 营收 {_pct(rev_g)}",
        f"净利润 {_amt(net_profit)}，经营现金流 {_amt(cash)}",
        f"毛利率 {_pct(gm_prev)} → {_pct(gm)}",
        f"合同负债 {_amt(contract_prev)} → {_amt(contract)}",
    ]
    return [
        {"名称": name, "触发": bool(hit), "权重": weight, "跟踪": track, "说明": detail}
        for (name, weight, track), hit, detail in zip(_SIGNALS, fired, details)
    ]


def trend_panel(pdf_data: dict) -> dict:
    """预警分 = 触发信号权重之和（满分 100）。"""
    signals = divergence_signals(build_data(pdf_data))
    score = sum(s["权重"] for s in signals if s["触发"])
    level = "高" if score >= 60 else ("中" if score >= 30 else "低")
    track = sorted({t for s in signals if s["触发"] for t in s["跟踪"].split("、")})
    return {
        "score": score, "level": level, "signals": signals,
        "track": track, "series": build_series(pdf_data),
    }


def backtest(series: pd.DataFrame, horizon: int = 1) -> dict:
    """按历史序列回测每条信号：触发后下期净利润/营收是否下滑。

    series 按时间升序。少于 3 期无法构成“触发→结果”，返回 insufficient。
    """
    if len(series) < 3:
        return {"insufficient": True, "periods": len(series), "rows": []}

    stats = {name: {"触发": 0, "命中": 0} for name, _, _ in _SIGNALS}
    for i in range(1, len(series) - horizon):
        cur, prev = series.iloc[i], series.iloc[i - 1]
        nxt = series.iloc[i + horizon]
        data = {
            "营业收入": cur["营业收入"], "营业收入_上期": prev["营业收入"],
            "营业成本": cur["营业成本"], "营业成本_上期": prev["营业成本"],
            "应收账款": cur["应收账款"], "应收账款_上期": prev["应收账款"],
            "存货": cur["存货"], "存货_上期": prev["存货"],
            "合同负债": cur["合同负债"], "合同负债_上期": prev["合同负债"],
            "净利润": cur["净利润"],
            "经营活动产生的现金流量净额": cur["经营活动现金流量净额"],
        }
        if cur["净利润"] is not None and nxt["净利润"] is not None:
            worsened = nxt["净利润"] < cur["净利润"]
        elif cur["营业收入"] is not None and nxt["营业收入"] is not None:
            worsened = nxt["营业收入"] < cur["营业收入"]
        else:
            continue
        for s in divergence_signals(data):
            if s["触发"]:
                stats[s["名称"]]["触发"] += 1
                stats[s["名称"]]["命中"] += bool(worsened)

    rows = [
        {"信号": name, "触发次数": v["触发"], "命中次数": v["命中"],
         "命中率": (v["命中"] / v["触发"]) if v["触发"] else None}
        for name, v in stats.items()
    ]
    return {"insufficient": False, "periods": len(series), "rows": rows}
