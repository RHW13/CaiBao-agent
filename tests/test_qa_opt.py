"""问答优化回归：BM25 排序、关键词降噪、多轮/跨期/跨公司、置信度、异常与性能。"""
import time

from core.notes_scan import build_notes
from core.qa import _keywords, answer, validate_answer

EMPTY = {
    "pages": [], "stmt_starts": {}, "traces": {},
    "balance_sheet": None, "income": None, "cashflow": None,
    "meta": {}, "warnings": [], "unknown_accounts": [],
}


def test_bm25_ranks_relevant_page_first():
    pages = [
        {"page": 1, "text": "无关内容" * 40},
        {"page": 6, "text": "管理层讨论与分析：报告期内毛利率提升，主要受产品结构影响。"},
        {"page": 7, "text": "风险因素" * 200},
    ]
    notes = build_notes(pages)
    hits = notes.search_ranked(["毛利率"], limit=3)
    assert hits and hits[0]["page"] == 6


def test_keywords_substring_dedup():
    keywords = _keywords("营业收入增长率")
    assert "营业收入" in keywords
    assert "营业" not in keywords
    assert "收入" not in keywords


def test_offtopic_question_refused(pdf_data, index):
    result = answer("今天天气怎么样", pdf_data, index=index, use_llm=False)
    assert result["route"] == "refuse"
    assert "与财报分析无关" in result["answer"]


def test_empty_document_refused():
    result = answer("营业收入是多少", EMPTY, use_llm=False)
    assert result["route"] == "refuse"
    assert "未解析到可检索内容" in result["answer"]


def test_followup_inherits_entity(pdf_data, index):
    history = [{"role": "user", "content": "净利润是多少？"}]
    result = answer("那去年呢", pdf_data, index=index, use_llm=False, history=history)
    assert result["route"] == "account"
    assert "净利润" in result["answer"]


def test_followup_without_history_not_answered(pdf_data, index):
    result = answer("那去年呢", pdf_data, index=index, use_llm=False)
    assert result["route"] != "account"


def test_period_previous_value(pdf_data, index):
    result = answer("去年营业收入是多少", pdf_data, index=index, use_llm=False)
    assert result["route"] == "account"
    assert "上期" in result["answer"]


def test_trend_limitation(pdf_data, index):
    result = answer("近三年营业收入是多少", pdf_data, index=index, use_llm=False)
    assert result["route"] == "account"
    assert "仅解析两期" in result["answer"]


def test_compare_with_peers(pdf_data, pdf_159, index):
    peers = [{"company": "样本159", "pdf_data": pdf_159}]
    result = answer("营业收入对比", pdf_data, index=index, use_llm=False, peers=peers)
    assert result["route"] == "compare"
    assert "样本159" in result["answer"]
    assert "排名" in result["answer"] or "数据缺失" in result["answer"]


def test_compare_without_peers_clarifies(pdf_data, index):
    result = answer("和同行比营业收入", pdf_data, index=index, use_llm=False)
    assert result["route"] == "clarify"
    assert "对标" in result["answer"]


def test_confidence_present(pdf_data, index):
    for question in ("营业收入是多少", "ROE是多少", "净利润下降的原因",
                     "今天天气怎么样", "毛利率和净利率分别是多少"):
        result = answer(question, pdf_data, index=index, use_llm=False)
        assert 0 <= result["confidence"] <= 1, question
        assert result["basis"], question


def test_validate_answer_unit_normalization():
    citations = [{"page": 12, "snippet": "营业收入 1,230,000,000.00 元"}]
    assert validate_answer("公司营业收入为 12.3 亿元 [第12页]。", citations)


def test_validate_answer_rejects_unknown_numbers():
    citations = [{"page": 12, "snippet": "营业收入 1,000,000,000.00 元"}]
    assert validate_answer("营业收入为 8.8 亿元 [第12页]。", citations) == ""


def test_ambiguous_ratio_clarifies(pdf_data, index):
    result = answer("毛利率和净利率分别是多少", pdf_data, index=index, use_llm=False)
    assert result["route"] == "clarify"
    assert "销售毛利率" in result["answer"]
    assert "销售净利率" in result["answer"]


def test_performance_budget(pdf_data, index):
    questions = ("营业收入是多少", "净利润是多少", "ROE是多少", "流动比率是多少",
                 "净利润下降的原因", "公司的主营业务是什么")
    answer(questions[0], pdf_data, index=index, use_llm=False)   # 预热缓存
    start = time.perf_counter()
    for _ in range(3):
        for question in questions:
            answer(question, pdf_data, index=index, use_llm=False)
    assert time.perf_counter() - start < 5.0
