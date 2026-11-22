"""自然语言 -> 公式 AST JSON（DeepSeek 优先，规则模板兜底）。"""
import json
import re

from . import config
from .evaluator import parse_formula, validate_ast, FormulaError

_SYS_PROMPT = (
    "你是财报指标公式解析器。把用户描述转化为 JSON AST，只能使用如下结构：\n"
    '变量 {"var": "科目名"}、常量 {"const": 1.23}、二元运算 {"op": "+|-|*|/", "left": ..., "right": ...}、'
    '函数 {"func": "sum|avg|yoy|nz", "args": [...]}（nz=缺失按0计）。\n'
    "科目名必须使用以下规范键，后缀约定：无后缀=本期值，_上期=上期值，_平均=(本期+上期)/2。\n"
    "可用科目键：货币资金、交易性金融资产、应收账款、预付款项、存货、一年内到期的非流动资产、"
    "流动资产合计、固定资产、资产总计、短期借款、应付账款、流动负债合计、非流动负债合计、负债合计、"
    "所有者权益合计、归母所有者权益合计、股本、营业收入、营业成本、税金及附加、销售费用、管理费用、"
    "研发费用、财务费用、利息费用、投资收益、营业利润、利润总额、所得税费用、净利润、归母净利润、"
    "基本每股收益、销售商品提供劳务收到的现金、经营活动产生的现金流量净额。\n"
    "口径：周转类与 ROE 的分母用 _平均；比率类若为存量对比取期末数。"
    "报表空白科目（如交易性金融资产）可用 nz 包裹。\n"
    "只输出 JSON，不要任何解释或代码块标记。"
)


def _ast_to_text(node) -> str:
    """把 AST JSON 还原为可读表达式（用于界面核对）。"""
    if not isinstance(node, dict):
        return ""
    if "var" in node:
        return str(node["var"])
    if "const" in node:
        return f"{node['const']:g}"
    if "op" in node:
        return f"({_ast_to_text(node['left'])} {node['op']} {_ast_to_text(node['right'])})"
    if "func" in node:
        return f"{node['func']}(" + ", ".join(_ast_to_text(a) for a in node["args"]) + ")"
    return ""


def _extract_json(content: str):
    """从 LLM 输出中提取 JSON 字符串。"""
    if not content:
        return None
    content = content.strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return content[start : end + 1]
    return None


def _llm_ast(client, text: str):
    try:
        resp = client.chat.completions.create(
            model=config.get_llm_model(),
            messages=[
                {"role": "system", "content": _SYS_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content
        json_str = _extract_json(content)
        if not json_str:
            return None
        # 模型输出属于外部数据：先按白名单校验重建 AST，非法结构直接放弃，
        # 由 nl_to_ast 回退到规则通道，避免异常结构进入求值器。
        return validate_ast(json.loads(json_str))
    except Exception:
        return None


# 常见指标 -> 规范公式（规则兜底）
_RULES = {
    "净资产收益率": "净利润 / 所有者权益合计_平均",
    "roe": "净利润 / 所有者权益合计_平均",
    "流动比率": "流动资产合计 / 流动负债合计",
    "速动比率": "(流动资产合计 - 存货) / 流动负债合计",
    "现金比率": "货币资金 / 流动负债合计",
    "资产负债率": "负债合计 / 资产总计",
    "产权比率": "负债合计 / 所有者权益合计",
    "毛利率": "(营业收入 - 营业成本) / 营业收入",
    "营业利润率": "营业利润 / 营业收入",
    "净利率": "净利润 / 营业收入",
    "净利润率": "净利润 / 营业收入",
    "总资产周转率": "营业收入 / 资产总计_平均",
    "存货周转率": "营业成本 / 存货_平均",
    "应收账款周转率": "营业收入 / 应收账款_平均",
    "总资产收益率": "净利润 / 资产总计_平均",
    "roa": "净利润 / 资产总计_平均",
}

# 自然语言中的「平均X」-> 规范键
_AVG_MAP = {
    "平均净资产": "所有者权益合计_平均",
    "平均总资产": "资产总计_平均",
    "平均资产": "资产总计_平均",
    "平均流动资产": "流动资产合计_平均",
    "平均存货": "存货_平均",
    "平均应收账款": "应收账款_平均",
}


def _extract_formula(text: str) -> str:
    """取 '=' 或 '：' 之后的公式片段。"""
    for sep in ("=", "："):
        if sep in text:
            return text.split(sep)[-1].strip()
    return text.strip()


def _match_rule(text: str) -> str:
    lowered = text.lower()
    for kw, formula in _RULES.items():
        if kw in lowered:
            return formula
    return ""


def _apply_avg_map(formula: str) -> str:
    for k, v in _AVG_MAP.items():
        formula = formula.replace(k, v)
    return formula


def nl_to_ast(text: str) -> dict:
    """自然语言/公式 -> {ast, source, formula, hint}。"""
    t = str(text or "").strip()
    result = {"ast": None, "source": "rule", "formula": "", "hint": ""}
    if not t:
        result["hint"] = "请输入公式。"
        return result

    # 1) LLM 优先
    client = config.get_llm_client()
    if client is not None:
        ast_node = _llm_ast(client, t)
        if ast_node is not None:
            result["ast"] = ast_node
            result["source"] = "llm"
            result["formula"] = _ast_to_text(ast_node) or _extract_formula(t)
            return result

    # 2) 规则模板兜底
    formula = _match_rule(t)
    if not formula:
        formula = _apply_avg_map(_extract_formula(t))
    try:
        ast_node = parse_formula(formula)
        result["ast"] = ast_node
        result["formula"] = formula
        result["hint"] = "（规则解析" + ("，已映射平均科目" if "_平均" in formula else "") + "）"
    except FormulaError as e:
        result["hint"] = str(e) + " 请改写为「指标 = 公式」，例如：ROE = 净利润 / 平均净资产"
    return result
