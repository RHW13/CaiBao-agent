"""Day 7/9：语义问答验收（20 类高频问题 + 无依据拒答）。"""
from core.qa import answer

QUESTIONS = [
    "营业收入是多少？", "净利润是多少？", "归母净利润是多少？", "总资产是多少？",
    "负债合计是多少？", "所有者权益合计是多少？", "货币资金余额是多少？",
    "应收账款是多少？", "存货是多少？", "经营活动产生的现金流量净额是多少？",
    "流动比率是多少？", "速动比率是多少？", "资产负债率是多少？",
    "销售毛利率是多少？", "销售净利率是多少？", "ROE是多少？", "ROA是多少？",
    "存货周转率是多少？", "应收账款周转率是多少？", "每股收益是多少？",
]

EMPTY = {
    "pages": [{"page": 1, "words": [], "text": "本文件仅用于测试，不含任何财务信息。"}],
    "stmt_starts": {}, "traces": {},
    "balance_sheet": None, "income": None, "cashflow": None,
    "meta": {}, "warnings": [], "unknown_accounts": [],
}


def test_numeric_questions_routed_to_calculator(pdf_data, index):
    results = [answer(q, pdf_data, index=index, use_llm=False) for q in QUESTIONS]
    assert all(r["route"] in ("account", "ratio") for r in results)
    answered = sum(1 for r in results if r.get("value") is not None)
    assert answered >= 18, f"仅 {answered}/20 题给出数值"
    assert all("未找到该科目" not in r["answer"] for r in results)


def test_every_text_answer_has_citations(pdf_data, index):
    """无引用不出数：凡走文本通道，必须带页码引用。"""
    for q in ("公司的主营业务是什么？", "核心竞争力有哪些？"):
        r = answer(q, pdf_data, index=index, use_llm=False)
        if r["route"] == "text":
            assert r["citations"], q
            total = len(pdf_data["pages"])
            assert all(1 <= c["page"] <= total for c in r["citations"])


def test_refuse_without_evidence():
    for q in ("公司董事长的姓名是什么？", "主要竞争对手的市场份额是多少？",
              "2027年的经营计划是什么？"):
        r = answer(q, EMPTY, use_llm=False)
        assert r["route"] == "refuse", q
        assert r["citations"] == []
        assert "建议查阅" in r["answer"]


def test_empty_question_refused(pdf_data):
    assert answer("  ", pdf_data, use_llm=False)["route"] == "refuse"
