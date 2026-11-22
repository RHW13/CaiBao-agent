"""3 年确定性外推、情景模拟与简化两阶段 DCF。

不引入 Prophet：以最新一期同比增速为基准外推，增速由界面滑块调整。
DCF 以经营活动现金流净额近似 FCF，结果用于情景比较，不作为投资建议。
"""
import pandas as pd

from .ratios import build_data


def _growth(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev)


def forecast(pdf_data: dict, revenue_growth: float = None,
             profit_growth: float = None, years: int = 3) -> dict:
    """按给定增速外推 3 年；增速缺省时取最新一期同比。"""
    data = build_data(pdf_data)
    rev = data.get("营业收入")
    profit = data.get("净利润")
    cash = data.get("经营活动产生的现金流量净额")

    rev_g = revenue_growth if revenue_growth is not None else _growth(
        rev, data.get("营业收入_上期"))
    profit_g = profit_growth if profit_growth is not None else _growth(
        profit, data.get("净利润_上期"))
    rev_missing = rev_g is None
    profit_missing = profit_g is None
    rev_g = 0.0 if rev_g is None else rev_g
    profit_g = 0.0 if profit_g is None else profit_g
    cash_g = rev_g  # 经营现金流按营业收入增速外推，与 DCF 假设保持一致

    rows = [{"年度": "基期", "营业收入": rev, "净利润": profit, "经营现金流": cash}]
    for i in range(1, years + 1):
        rev = None if rev is None else rev * (1 + rev_g)
        profit = None if profit is None else profit * (1 + profit_g)
        cash = None if cash is None else cash * (1 + cash_g)
        rows.append({"年度": f"T+{i}", "营业收入": rev, "净利润": profit, "经营现金流": cash})

    return {
        "rows": pd.DataFrame(rows),
        "revenue_growth": rev_g,
        "profit_growth": profit_g,
        "cash_growth": cash_g,
        "revenue_growth_missing": rev_missing,
        "profit_growth_missing": profit_missing,
    }


def dcf(pdf_data: dict, wacc: float = 0.10, terminal_growth: float = 0.03,
        growth_adjust: float = 0.0, years: int = 3) -> dict:
    """两阶段 DCF：3 年明确预测期 + 永续期。"""
    if wacc <= terminal_growth:
        return {"ok": False, "reason": "WACC 必须大于永续增长率。"}

    data = build_data(pdf_data)
    cash = data.get("经营活动产生的现金流量净额")
    profit = data.get("净利润")
    base = cash if cash is not None else profit
    if base is None:
        return {"ok": False, "reason": "缺少经营现金流与净利润，无法估值。"}

    growth_missing = False
    growth = _growth(data.get("营业收入"), data.get("营业收入_上期"))
    if growth is None:
        growth = _growth(cash, data.get("经营活动产生的现金流量净额_上期"))
    if growth is None:
        growth = _growth(profit, data.get("净利润_上期"))
    if growth is None:
        growth_missing = True
        growth = 0.0
    growth = max(-0.30, min(0.30, growth + growth_adjust))

    flows = [base * (1 + growth) ** t for t in range(1, years + 1)]
    pv_stage1 = sum(cf / (1 + wacc) ** t for t, cf in enumerate(flows, start=1))
    terminal = flows[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal / (1 + wacc) ** years
    enterprise = pv_stage1 + pv_terminal

    debt = sum(data.get(k) or 0.0 for k in
               ("短期借款", "长期借款", "应付债券", "租赁负债"))
    equity = enterprise - debt + (data.get("货币资金") or 0.0)
    shares = data.get("股本")
    per_share = equity / shares if shares else None

    warnings = []
    if base < 0:
        warnings.append("基期经营现金流为负，DCF 结果参考意义有限。")
    if growth_missing:
        warnings.append("同比增速均缺失，按 0 增长估值。")
    if equity is not None and equity < 0:
        warnings.append("股权价值为负，当前现金流/资本结构不支持 DCF 估值。")
    return {
        "ok": True, "growth": growth, "base_fcf": base,
        "flows": flows, "pv_stage1": pv_stage1,
        "terminal_value": terminal, "pv_terminal": pv_terminal,
        "enterprise": enterprise, "net_debt": debt - (data.get("货币资金") or 0.0),
        "equity": equity, "per_share": per_share,
        "growth_missing": growth_missing,
        "warning": "".join(warnings),
    }
