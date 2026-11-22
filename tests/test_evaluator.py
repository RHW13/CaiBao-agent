"""Day 2/9：白名单公式求值器。"""
import pytest

from core.evaluator import FormulaError, eval_text, evaluate, parse_formula, validate_ast


def test_basic_arithmetic():
    ast = parse_formula("(净利润 - 100) / 2")
    assert evaluate(ast, {"净利润": 300}) == 100


def test_functions():
    data = {"净利润": 120.0, "营业收入": 1000.0,
            "净利润_上期": 100.0, "营业收入_上期": 900.0}
    assert eval_text(data, "nz(交易性金融资产)") == 0
    assert abs(eval_text(data, "yoy(净利润, 净利润_上期)") - 0.2) < 1e-9
    assert eval_text(data, "avg(净利润, 净利润_上期)") == 110.0
    assert eval_text(data, "abs(净利润 - 200)") == 80.0


@pytest.mark.parametrize("formula", [
    "__import__('os').system('echo hi')",
    "净利润.__class__",
    "open('/etc/passwd')",
    "净利润 ** 2",
])
def test_unsafe_formula_rejected(formula):
    with pytest.raises(FormulaError):
        parse_formula(formula)


def test_division_by_zero_returns_none():
    assert eval_text({"净利润": 10}, "净利润 / 0") is None


def test_validate_ast_accepts_llm_shape():
    node = {"op": "/", "left": {"var": "净利润"}, "right": {"var": "营业收入"}}
    ast_node = validate_ast(node)
    assert evaluate(ast_node, {"净利润": 10.0, "营业收入": 100.0}) == 0.1
    assert validate_ast({"func": "nz", "args": [{"var": "商誉"}]}) == {
        "func": "nz", "args": [{"var": "商誉"}]}


@pytest.mark.parametrize("node", [
    "净利润 / 营业收入",                 # 非 dict 结构
    {"var": 123},                        # 变量名非字符串
    {"var": "  "},                       # 变量名为空
    {"const": "1"},                      # 常量非数字
    {"const": True},                     # 布尔被当作数值
    {"op": "**", "left": {"const": 1}, "right": {"const": 2}},
    {"op": "+", "left": {"const": 1}},   # 缺少右操作数
    {"func": "exec", "args": [{"const": 1}]},
    {"func": "yoy", "args": [{"const": 1}]},          # 参数个数不符
    {"func": "sum", "args": []},                       # 空参数
    {"unknown": 1},
])
def test_validate_ast_rejects_malformed(node):
    with pytest.raises(FormulaError):
        validate_ast(node)


def test_validate_ast_rejects_deep_nesting():
    node = {"const": 1}
    for _ in range(200):
        node = {"op": "+", "left": node, "right": {"const": 1}}
    with pytest.raises(FormulaError):
        validate_ast(node)
