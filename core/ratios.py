"""36 个财务比率计算 + 杜邦分析（口径依据《财务比率分析公式手册》，中国教材主流口径）。

指标公式用「规范科目键」引用，后缀约定：
  - 无后缀   -> 本期值
  - _上期     -> 上期值
  - _平均     -> (本期 + 上期) / 2
  - nz(...)  -> 科目缺失（报表空白项）时按 0 计

「科目存在但金额空白」（如某公司无非流动负债，报表该行两个金额格均空白）由
build_data 按 0 补入，与「科目缺失」区分：前者参与计算，后者标注数据缺失。

字段说明：
  importance -> 核心 / 常用 / 了解（手册重要性标注）
  pct        -> 结果按百分比展示（计算值 × 100）
  scale      -> 结果缩放倍数（如绝对额换算为亿元）
  need       -> 需外部输入的数据（如股价），缺失时标注原因
"""
import pandas as pd

from .calibers import CALIBERS, get_caliber
from .evaluator import parse_formula, evaluate, FormulaError, iter_vars

_DAYS = 360.0

# 36 个指标（按手册七大板块顺序）
INDICATORS = [
    # ---------- 一、短期偿债能力（5） ----------
    {"name": "流动比率", "category": "短期偿债", "importance": "核心",
     "formula": "流动资产合计 / 流动负债合计", "unit": "倍"},
    {"name": "速动比率", "category": "短期偿债", "importance": "核心",
     "caliber": "速动比率", "unit": "倍"},
    {"name": "现金比率", "category": "短期偿债", "importance": "常用",
     "formula": "(货币资金 + nz(交易性金融资产)) / 流动负债合计", "unit": "倍"},
    {"name": "营运资本", "category": "短期偿债", "importance": "常用",
     "formula": "流动资产合计 - 流动负债合计", "unit": "亿元", "scale": 1e-8},
    {"name": "现金流量比率", "category": "短期偿债", "importance": "常用",
     "formula": "经营活动产生的现金流量净额 / 流动负债合计", "unit": "倍"},

    # ---------- 二、长期偿债能力（7） ----------
    {"name": "资产负债率", "category": "长期偿债", "importance": "核心",
     "formula": "负债合计 / 资产总计", "unit": "%", "pct": True},
    {"name": "产权比率", "category": "长期偿债", "importance": "核心",
     "formula": "负债合计 / 所有者权益合计", "unit": "%", "pct": True},
    {"name": "权益乘数", "category": "长期偿债", "importance": "核心",
     "formula": "资产总计 / 所有者权益合计", "unit": "倍"},
    {"name": "长期资本负债率", "category": "长期偿债", "importance": "常用",
     "formula": "非流动负债合计 / (非流动负债合计 + 所有者权益合计)", "unit": "%", "pct": True},
    {"name": "利息保障倍数", "category": "长期偿债", "importance": "核心",
     "formula": "(利润总额 + abs(利息费用)) / abs(利息费用)", "unit": "倍"},
    {"name": "现金流量利息保障倍数", "category": "长期偿债", "importance": "常用",
     "formula": "经营活动产生的现金流量净额 / abs(利息费用)", "unit": "倍"},
    {"name": "现金流量债务比", "category": "长期偿债", "importance": "了解",
     "formula": "经营活动产生的现金流量净额 / 负债合计", "unit": "%", "pct": True},

    # ---------- 三、营运能力（6） ----------
    {"name": "应收账款周转率", "category": "营运能力", "importance": "核心",
     "formula": "营业收入 / 应收账款_平均", "unit": "次"},
    {"name": "存货周转率", "category": "营运能力", "importance": "核心",
     "formula": "营业成本 / 存货_平均", "unit": "次"},
    {"name": "流动资产周转率", "category": "营运能力", "importance": "常用",
     "formula": "营业收入 / 流动资产合计_平均", "unit": "次"},
    {"name": "固定资产周转率", "category": "营运能力", "importance": "常用",
     "formula": "营业收入 / 固定资产_平均", "unit": "次"},
    {"name": "总资产周转率", "category": "营运能力", "importance": "核心",
     "formula": "营业收入 / 资产总计_平均", "unit": "次"},
    {"name": "营业周期", "category": "营运能力", "importance": "核心",
     "formula": f"{_DAYS} / (营业成本 / 存货_平均) + {_DAYS} / (营业收入 / 应收账款_平均)",
     "unit": "天"},

    # ---------- 四、盈利能力（6） ----------
    {"name": "销售毛利率", "category": "盈利能力", "importance": "核心",
     "formula": "(营业收入 - 营业成本) / 营业收入", "unit": "%", "pct": True},
    {"name": "销售净利率", "category": "盈利能力", "importance": "核心",
     "formula": "净利润 / 营业收入", "unit": "%", "pct": True},
    {"name": "总资产净利率(ROA)", "category": "盈利能力", "importance": "核心",
     "formula": "净利润 / 资产总计_平均", "unit": "%", "pct": True},
    {"name": "净资产收益率(ROE)", "category": "盈利能力", "importance": "核心",
     "caliber": "净资产收益率(ROE)", "unit": "%", "pct": True},
    {"name": "成本费用利润率", "category": "盈利能力", "importance": "常用",
     "formula": ("利润总额 / (营业成本 + nz(税金及附加) + nz(销售费用) + "
                 "nz(管理费用) + nz(研发费用) + nz(财务费用))"),
     "unit": "%", "pct": True},
    {"name": "每股收益(EPS)", "category": "盈利能力", "importance": "核心",
     "formula": "基本每股收益", "unit": "元/股"},

    # ---------- 五、盈利质量（5） ----------
    {"name": "净现比(盈余现金保障倍数)", "category": "盈利质量", "importance": "核心",
     "formula": "经营活动产生的现金流量净额 / 净利润", "unit": "倍"},
    {"name": "销售收现比率", "category": "盈利质量", "importance": "核心",
     "formula": "销售商品提供劳务收到的现金 / 营业收入", "unit": "%", "pct": True},
    {"name": "现金营运指数", "category": "盈利质量", "importance": "常用",
     "formula": "经营活动产生的现金流量净额 / (净利润 + 折旧摊销)",
     "unit": "倍", "need": ["折旧摊销"]},
    {"name": "经营现金流对核心利润的保障倍数", "category": "盈利质量", "importance": "了解",
     "formula": ("经营活动产生的现金流量净额 / (营业收入 - 营业成本 - nz(税金及附加) "
                 "- nz(销售费用) - nz(管理费用) - nz(财务费用))"),
     "unit": "倍"},
    {"name": "主营业务现金比率", "category": "盈利质量", "importance": "了解",
     "formula": "经营活动产生的现金流量净额 / 营业收入", "unit": "%", "pct": True},

    # ---------- 六、市场价值比率（7） ----------
    {"name": "市盈率(P/E)", "category": "市场价值", "importance": "核心",
     "formula": "每股市价 / 基本每股收益", "unit": "倍", "need": ["每股市价"]},
    {"name": "市净率(P/B)", "category": "市场价值", "importance": "核心",
     "formula": "每股市价 / (归母所有者权益合计 / 股本)", "unit": "倍", "need": ["每股市价"]},
    {"name": "市销率(P/S)", "category": "市场价值", "importance": "常用",
     "formula": "每股市价 / (营业收入 / 股本)", "unit": "倍", "need": ["每股市价"]},
    {"name": "每股净资产", "category": "市场价值", "importance": "核心",
     "formula": "归母所有者权益合计 / 股本", "unit": "元/股"},
    {"name": "每股股利", "category": "市场价值", "importance": "常用",
     "formula": "每股股利", "unit": "元/股", "need": ["每股股利"]},
    {"name": "股利支付率", "category": "市场价值", "importance": "核心",
     "formula": "每股股利 / 基本每股收益", "unit": "%", "pct": True, "need": ["每股股利"]},
    {"name": "股息率", "category": "市场价值", "importance": "常用",
     "formula": "每股股利 / 每股市价", "unit": "%", "pct": True,
     "need": ["每股市价", "每股股利"]},
]


def _is_none(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def build_data(statements: dict, extra: dict = None, approx: set = None) -> dict:
    """把三表整理为 {科目键: 数值}，含 _上期 / _平均 后缀键。

    extra：外部补充数据（如每股市价、每股股利、折旧摊销），直接并入。
    approx：可选，传入 set 时收集「单期近似」的 _平均 键（上期/本期缺一侧，按单期兜底）。
    """
    data = {}
    specs = [
        ("balance_sheet", "本期期末", "上期期末"),
        ("income", "本期金额", "上期金额"),
        ("cashflow", "本期金额", "上期金额"),
    ]
    for stmt_key, col_cur, col_prev in specs:
        df = statements.get(stmt_key)
        if df is None:
            continue
        for _, r in df.iterrows():
            key = r["科目"]
            cur = r[col_cur]
            prev = r[col_prev]
            if key not in data or (data.get(key) is None and not _is_none(cur)):
                data[key] = None if _is_none(cur) else float(cur)
                data[key + "_上期"] = None if _is_none(prev) else float(prev)
                if not _is_none(cur) and not _is_none(prev):
                    data[key + "_平均"] = (float(cur) + float(prev)) / 2.0
                elif not _is_none(cur):
                    data[key + "_平均"] = float(cur)
                    if approx is not None:
                        approx.add(key + "_平均")
                elif not _is_none(prev):
                    data[key + "_平均"] = float(prev)
                    if approx is not None:
                        approx.add(key + "_平均")
    if extra:
        for k, v in extra.items():
            if v is not None:
                data[k] = float(v)
    _apply_blank_accounts(statements, data)
    _normalize_signs(data)
    return data


_NEG_COST_KEYS = ("营业成本",)


def _normalize_signs(data: dict) -> None:
    """把以负数列示的成本类科目转为正数。

    部分报表把营业成本作为收入抵减项写成负数（如「(194,366,771)」），
    而比率公式按正数成本计算，若不统一会导致毛利率等指标失真。
    """
    for key in _NEG_COST_KEYS:
        for suffix in ("", "_上期", "_平均"):
            k = key + suffix
            v = data.get(k)
            if v is not None and v < 0:
                data[k] = -v


def _apply_blank_accounts(statements: dict, data: dict) -> None:
    """把「科目存在、金额空白」的标准科目按 0 补入 data。

    中国财报中空白单元格即无余额（0），而非「未披露」。解析层已把这类科目收集在
    blank_accounts 中且不写入报表行，此处仅在该科目没有其它非空取值时补 0，
    既不改变报表原样展示，也让依赖该科目的比率可正常计算（如非流动负债合计为空
    时长期资本负债率应为 0%）。
    """
    blanks = (statements or {}).get("blank_accounts")
    if not isinstance(blanks, dict):
        return
    for names in blanks.values():
        if not isinstance(names, (list, tuple, set)):
            continue
        for name in names:
            if not name or not isinstance(name, str) or data.get(name) is not None:
                continue
            data[name] = 0.0
            if data.get(name + "_上期") is None:
                data[name + "_上期"] = 0.0
            if data.get(name + "_平均") is None:
                data[name + "_平均"] = 0.0


def _missing_vars(formula: str, data: dict) -> list:
    """返回公式中缺失（不在 data 或值为 None）的变量名（忽略 nz 包裹的变量）。"""
    try:
        ast_node = parse_formula(formula)
    except FormulaError:
        return []
    names = dict.fromkeys(iter_vars(ast_node, skip_nz=True))
    return [n for n in names if data.get(n) is None]


def _spec_formula(ind: dict, caliber):
    """返回 (公式, 口径标签)。

    caliber 可为字符串（全局口径）或 dict（{指标名: 口径名} 逐指标指定）。
    """
    key = ind.get("caliber")
    if key is None:
        return ind["formula"], "标准"
    variants = CALIBERS.get(key, {})
    chosen = caliber.get(key, "标准") if isinstance(caliber, dict) else caliber
    label = chosen if chosen in variants else next(iter(variants), "标准")
    return get_caliber(key, chosen), label


def compute_ratios(statements: dict, caliber="标准", extra: dict = None) -> pd.DataFrame:
    """计算 36 个财务比率，返回 DataFrame[板块, 指标, 重要性, 数值, 单位, 口径, 备注]。

    caliber：全局口径字符串，或 {指标名: 口径名} 的逐指标口径映射。
    extra：外部补充数据（每股市价、每股股利、折旧摊销等）。
    """
    approx = set()
    data = build_data(statements, extra, approx=approx)
    rows = []
    for ind in INDICATORS:
        formula, cal_label = _spec_formula(ind, caliber)
        scale = ind.get("scale", 1.0)
        if ind.get("pct", False):
            scale *= 100.0

        note = ""
        value = None
        need = ind.get("need") or []
        lacking = [n for n in need if data.get(n) is None]
        if lacking:
            note = "需补充：" + "、".join(lacking)
        else:
            try:
                value = evaluate(parse_formula(formula), data)
            except FormulaError as e:
                note = f"公式错误：{e}"
            if value is None and not note:
                missing = _missing_vars(formula, data)
                note = ("数据缺失：" + "、".join(missing)) if missing else "分母为 0 或无法计算"
        if value is not None and not note:
            ast_node = parse_formula(formula)
            used_approx = [v for v in dict.fromkeys(iter_vars(ast_node, skip_nz=False))
                           if v in approx]
            if used_approx:
                note = "单期近似：" + "、".join(used_approx)
        if value is not None:
            value = value * scale

        rows.append({
            "板块": ind["category"],
            "指标": ind["name"],
            "重要性": ind["importance"],
            "数值": value,
            "单位": ind["unit"],
            "口径": cal_label,
            "备注": note,
        })
    return pd.DataFrame(rows)


# ---------- 杜邦分析 ----------

# 杜邦三因素：(比率名, 本期公式, 上期公式, 驱动因素)
# 手册恒等式 ROE = (净利润÷营业收入) × (营业收入÷平均资产总额) × (平均资产总额÷平均股东权益)
_DUPONT = [
    ("销售净利率", "净利润 / 营业收入", "净利润_上期 / 营业收入_上期", "盈利能力"),
    ("总资产周转率", "营业收入 / 资产总计_平均", "营业收入_上期 / 资产总计_上期", "营运能力"),
    ("权益乘数", "资产总计_平均 / 所有者权益合计_平均",
     "资产总计_上期 / 所有者权益合计_上期", "财务杠杆"),
]


def dupont_analysis(statements: dict) -> pd.DataFrame:
    """杜邦三因素分解（本期 / 上期 / 变动）。

    本期严格按手册恒等式口径：净利率当期、周转率与权益乘数均用平均数。
    单份年报仅有期初、期末两期数，故上期因素以期末数近似，用于趋势判断。
    """
    data = build_data(statements)
    rows = []
    for name, cur_f, prev_f, driver in _DUPONT:
        cur = evaluate(parse_formula(cur_f), data)
        prev = evaluate(parse_formula(prev_f), data)
        rows.append({
            "驱动因素": driver,
            "比率": name,
            "本期": cur,
            "上期": prev,
            "变动": None if (cur is None or prev is None) else cur - prev,
        })
    return pd.DataFrame(rows)


def dupont_roe(statements: dict) -> dict:
    """返回本期杜邦三因素乘积与直接计算的 ROE，用于校验恒等式。"""
    data = build_data(statements)
    factors = {}
    for name, cur_f, _, _ in _DUPONT:
        factors[name] = evaluate(parse_formula(cur_f), data)
    product = None
    if all(v is not None for v in factors.values()):
        product = factors["销售净利率"] * factors["总资产周转率"] * factors["权益乘数"]
    direct = evaluate(parse_formula("净利润 / 所有者权益合计_平均"), data)
    return {"factors": factors, "product": product, "roe": direct}
