"""多公司横向对标：批量结果 → 指标对比表 + 均值/中位数/分位 + 柱状图/雷达图。"""
import pandas as pd
import plotly.express as px

from .ratios import build_data, compute_ratios
from .scores import five_dimension_scores

# 对标展示的核心指标（按手册重要性筛选）
KEY_INDICATORS = [
    "流动比率", "速动比率", "资产负债率", "销售毛利率", "销售净利率",
    "净资产收益率(ROE)", "总资产周转率", "存货周转率", "净现比(盈余现金保障倍数)",
]

# 各对标指标的单位（用于在对比表/汇总表中标注，避免 % 与倍等混排误读）
_KEY_UNITS = {
    "流动比率": "倍", "速动比率": "倍", "资产负债率": "%",
    "销售毛利率": "%", "销售净利率": "%", "净资产收益率(ROE)": "%",
    "总资产周转率": "次", "存货周转率": "次", "净现比(盈余现金保障倍数)": "倍",
}


def compare(results: dict):
    """results: {公司名: pdf_data} → (明细表, 汇总表)。

    明细表 index=指标、columns=公司；汇总表给出均值/中位数/四分位与样本数。
    """
    columns = {}
    for name, data in results.items():
        ratios = compute_ratios(data)
        columns[name] = ratios.set_index("指标")["数值"]
    detail = pd.DataFrame(columns)
    detail = detail.reindex([i for i in KEY_INDICATORS if i in detail.index])
    # 指标名附加单位，避免无单位混排导致误读
    detail = detail.rename(index=lambda i: f"{i}（{_KEY_UNITS.get(i, '')}）")

    rows = []
    for indicator, row in detail.iterrows():
        base_name = indicator.split("（")[0]
        values = row.dropna()
        rows.append({
            "指标": base_name,
            "单位": _KEY_UNITS.get(base_name, ""),
            "均值": values.mean() if len(values) else None,
            "中位数": values.median() if len(values) else None,
            "25分位": values.quantile(0.25) if len(values) else None,
            "75分位": values.quantile(0.75) if len(values) else None,
            "样本数": len(values),
        })
    return detail, pd.DataFrame(rows)


def bar_chart(detail: pd.DataFrame, indicator: str):
    row = detail.loc[indicator]
    fig = px.bar(x=list(row.index), y=list(row.values), labels={"x": "公司", "y": indicator},
                 title=f"{indicator} 横向对比")
    fig.update_layout(height=360, margin={"l": 40, "r": 20, "t": 50, "b": 60})
    return fig


def score_matrix(results: dict) -> dict:
    """{公司名: pdf_data} → {公司名: {维度: 分}}，供雷达图使用。"""
    return {name: five_dimension_scores(build_data(data))[0] for name, data in results.items()}
