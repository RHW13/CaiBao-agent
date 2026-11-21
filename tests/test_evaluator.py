"""Day 2/9：白名单公式求值器。"""
import pytest

from core.evaluator import FormulaError, eval_text, evaluate, parse_formula


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
