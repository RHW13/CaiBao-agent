"""Day 2/9：比率引擎与关键科目。"""
from core.evaluator import eval_text
from core.ratios import compute_ratios


def test_key_accounts_present(data_map):
    for name in ("营业收入", "净利润", "资产总计", "负债合计", "所有者权益合计"):
        assert data_map.get(name) is not None, name
    assert data_map["资产总计"] > 0


def test_balance_identity(data_map):
    assets = data_map["资产总计"]
    claims = (data_map.get("负债合计") or 0) + (data_map.get("所有者权益合计") or 0)
    assert abs(assets - claims) / assets < 0.005


def test_ratio_coverage(pdf_data):
    ratios = compute_ratios(pdf_data)
    assert len(ratios) == 36
    assert ratios["数值"].notna().sum() >= 25


def test_interest_coverage_uses_abs():
    value = eval_text({"利息费用": -100.0, "利润总额": 50.0},
                      "(利润总额 + abs(利息费用)) / abs(利息费用)")
    assert abs(value - 1.5) < 1e-9


def test_missing_account_returns_none():
    assert eval_text({}, "净利润 / 营业收入") is None
