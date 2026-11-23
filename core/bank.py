"""银行业专项指标：三表口径计算 + 年报披露值抽取。

A 股银行报表没有「流动资产合计 / 存货 / 营业成本」等工商企业科目，
通用 36 指标中约半数结构性不可算。本模块在识别为银行报表时追加一组
银行专用指标，数据全部来自已解析的三表，另从管理层讨论正文抽取
不良率、拨备覆盖率、资本充足率等披露值，并标注页码来源。
"""
import re

KIND = "银行专项"
_PCT = 100.0

# 披露值：名称 -> (正则, 合理下限, 合理上限, 说明)
_DISCLOSED = [
    ("不良贷款率", re.compile(r"不良贷款率[^0-9%]{0,14}([0-9]+(?:\.[0-9]+)?)\s*%"),
     0.0, 15.0, "披露值"),
    ("拨备覆盖率", re.compile(r"拨备覆盖率[^0-9%]{0,14}([0-9]+(?:\.[0-9]+)?)\s*%"),
     50.0, 1500.0, "披露值"),
    ("资本充足率", re.compile(r"(?<!核心一级)(?<!一级)资本充足率[^0-9%]{0,12}([0-9]+(?:\.[0-9]+)?)\s*%"),
     5.0, 50.0, "披露值"),
    ("核心一级资本充足率", re.compile(r"核心一级资本充足率[^0-9%]{0,12}([0-9]+(?:\.[0-9]+)?)\s*%"),
     3.0, 40.0, "披露值"),
    ("净息差(披露)", re.compile(r"(?:净息差|净利息收益率)[^0-9%]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"),
     0.3, 8.0, "披露值"),
]


def is_bank(statements: dict) -> bool:
    """按报表科目特征识别银行：有贷款/存款科目，且没有保险业务收入。"""
    bs = statements.get("balance_sheet")
    if bs is None:
        return False
    names = set(bs["科目"])
    inc = statements.get("income")
    inc_names = set(inc["科目"]) if inc is not None else set()
    if inc_names & {"保险服务收入", "已赚保费", "保险业务收入"}:
        return False
    # 产业集团（含财务公司）也可能有贷款/存款科目，但同时有存货与营业成本
    if "存货" in names or "营业成本" in inc_names:
        return False
    return ("发放贷款和垫款" in names and "吸收存款" in names) or (
        "拆出资金" in names and "利息净收入" in inc_names)


def _v(data: dict, key: str):
    value = data.get(key)
    return None if value is None else float(value)


def _avg(data: dict, key: str):
    """返回 (平均值, 是否单期近似)。"""
    value = data.get(key + "_平均")
    if value is not None:
        return float(value), False
    value = data.get(key)
    return (float(value), True) if value is not None else (None, True)


def _row(name, value, unit, importance, note=""):
    return {"板块": KIND, "指标": name, "重要性": importance, "数值": value,
            "单位": unit, "口径": "标准", "备注": note}


def _ratio(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _asset_yield_base(data: dict):
    """生息资产近似值：贷款 + 金融投资 + 同业/拆出/买入返售 + 现金类。"""
    parts = ["发放贷款和垫款", "交易性金融资产", "债权投资", "其他债权投资",
             "拆出资金", "买入返售金融资产", "存放同业款项", "现金及存放中央银行款项"]
    total, approx, used = 0.0, False, 0
    for key in parts:
        value, single = _avg(data, key)
        if value is None:
            continue
        total += value
        approx = approx or single
        used += 1
    if used < 2 or _avg(data, "发放贷款和垫款")[0] is None:
        return None, True
    return total, approx


def bank_rows(statements: dict, data: dict) -> list:
    """银行专用指标（含披露值），返回与 ratios 相同结构的行列表。"""
    rows = []
    revenue = _v(data, "营业收入")
    net_interest = _v(data, "利息净收入")
    fee = _v(data, "手续费及佣金净收入")
    admin = _v(data, "业务及管理费")
    impair = _v(data, "信用减值损失")
    op_profit = _v(data, "营业利润")
    loans, loans_single = _avg(data, "发放贷款和垫款")
    deposits = _v(data, "吸收存款")
    assets, assets_single = _avg(data, "资产总计")
    equity, equity_single = _avg(data, "所有者权益合计")

    # 1. 净息差（近似）：利息净收入 / 生息资产平均
    base, base_single = _asset_yield_base(data)
    nim = _ratio(net_interest, base)
    if nim is not None:
        note = "生息资产按贷款、金融投资、同业及买入返售合计近似；报告期未年化"
        if base_single or _v(data, "利息净收入_上期") is None:
            note += "；单期近似"
        rows.append(_row("净息差(近似)", nim * _PCT, "%", "核心", note))

    # 2. 成本收入比：业务及管理费 / 营业收入
    cir = _ratio(abs(admin) if admin is not None else None, revenue)
    if cir is not None:
        rows.append(_row("成本收入比", cir * _PCT, "%", "核心",
                         "业务及管理费取绝对值；与年报披露口径一致"))

    # 3. 非利息收入占比
    if revenue not in (None, 0) and net_interest is not None:
        rows.append(_row("非利息收入占比", (revenue - net_interest) / revenue * _PCT,
                         "%", "常用", "营业收入扣除利息净收入"))

    # 4. 手续费及佣金净收入占比
    share = _ratio(fee, revenue)
    if share is not None:
        rows.append(_row("手续费及佣金净收入占比", share * _PCT, "%", "常用"))

    # 5. 存贷比
    ldr = _ratio(loans, deposits)
    if ldr is not None:
        rows.append(_row("存贷比", ldr * _PCT, "%", "核心",
                         "发放贷款和垫款 / 吸收存款（净额口径）"))

    # 6. 贷款资产比、7. 存款负债比
    lar = _ratio(loans, assets)
    if lar is not None:
        rows.append(_row("贷款资产比", lar * _PCT, "%", "常用"))
    dlr = _ratio(deposits, _v(data, "负债合计"))
    if dlr is not None:
        rows.append(_row("存款负债比", dlr * _PCT, "%", "常用"))

    # 8. 信用成本（报告期）：信用减值损失 / 平均贷款
    cost = _ratio(abs(impair) if impair is not None else None, loans)
    if cost is not None:
        note = "信用减值损失取绝对值；报告期未年化"
        if loans_single:
            note += "；贷款平均为单期近似"
        rows.append(_row("信用成本(报告期)", cost * _PCT, "%", "常用", note))

    # 9. 拨备前利润利润率
    if revenue not in (None, 0) and op_profit is not None:
        pre = op_profit + (abs(impair) if impair is not None else 0.0)
        rows.append(_row("拨备前利润利润率", pre / revenue * _PCT, "%", "常用",
                         "营业利润加回信用减值损失"))

    # 10. 营业利润率
    opm = _ratio(op_profit, revenue)
    if opm is not None:
        rows.append(_row("营业利润率", opm * _PCT, "%", "常用"))

    # 11. 利息净收入占比
    nis = _ratio(net_interest, revenue)
    if nis is not None:
        rows.append(_row("利息净收入占比", nis * _PCT, "%", "了解"))

    # 12. 每股净资产（银行口径）
    shares = _v(data, "股本")
    bvps = _ratio(equity, shares)
    if bvps is not None:
        note = "按平均股东权益计算" if not equity_single else ""
        if _v(data, "归母所有者权益合计") is None:
            note = (note + "；" if note else "") + "报表未单列归母权益，含少数股东权益"
        rows.append(_row("每股净资产(银行口径)", bvps, "元/股", "常用", note))

    rows.extend(_disclosed_rows(statements))
    return rows


def _disclosed_rows(statements: dict) -> list:
    """从正文抽取监管披露指标；同页有多值时取首个落在合理区间者。"""
    pages = statements.get("pages") or []
    rows = []
    for name, pattern, low, high, label in _DISCLOSED:
        hit = None
        for page in pages:
            match = pattern.search(page.get("text") or "")
            if not match:
                continue
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if low <= value <= high:
                hit = (value, page.get("page"))
                break
        if hit is not None:
            rows.append(_row(name, hit[0], "%", "常用" if "资本" in name else "核心",
                             f"{label}（第 {hit[1]} 页）"))
    return rows
