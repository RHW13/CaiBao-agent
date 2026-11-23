"""口径配置字典：同一指标的多口径并列（公式用规范科目键 + 白名单表达式）。

口径依据《财务比率分析公式手册》（中国教材主流口径）。
"""

CALIBERS = {
    "速动比率": {
        # 中国教材主流口径：速动资产 = 流动资产 - 存货
        "标准": "(流动资产合计 - 存货) / 流动负债合计",
        # 部分教材进一步扣除预付款项、一年内到期的非流动资产等
        "严格": "(流动资产合计 - 存货 - nz(预付款项) - nz(一年内到期的非流动资产)) / 流动负债合计",
    },
    "净资产收益率(ROE)": {
        # 主流口径：净利润 ÷ 平均股东权益
        "标准": "净利润 / 所有者权益合计_平均",
        # 部分教材采用期末股东权益
        "期末口径": "净利润 / 所有者权益合计",
    },
}


def get_caliber(indicator: str, caliber: str) -> str:
    """返回指定指标在指定口径下的公式字符串；口径不存在时回退到第一个口径。"""
    variants = CALIBERS.get(indicator)
    if not variants:
        return ""
    if caliber in variants:
        return variants[caliber]
    return next(iter(variants.values()))
