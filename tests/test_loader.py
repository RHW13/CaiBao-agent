"""Day 1/9：PDF 解析与三表定位冒烟测试。"""
from core.pdf_loader import _is_toc, detect_statement_pages

EXPECTED_688551 = {"balance_sheet": 104, "income": 108, "cashflow": 111}
EXPECTED_159 = {"balance_sheet": 77, "income": 81, "cashflow": 85}


def _page(text, nums=0):
    words = [{"text": f"{900000 + i * 1000}", "x0": 300 + i * 60,
              "x1": 350 + i * 60, "top": 100 + i * 12, "bottom": 110 + i * 12}
             for i in range(nums)]
    return {"page": 1, "text": text, "words": words}


def test_toc_by_keyword():
    page = _page("目录页次审计报告133已审计财务报表合并及公司资产负债表141", 20)
    assert _is_toc(page) is True


def test_toc_by_dot_leader():
    page = _page("合并资产负债表…………77 合并利润表…………81", 20)
    assert _is_toc(page) is True


def test_toc_page_is_never_picked_as_statement():
    # 无「目录」字样，但标题后紧跟页码：判为目录式引用，不应被选中
    page = _page("审计报告133合并及公司资产负债表141合并及公司利润表143", 20)
    assert detect_statement_pages([page]) == {
        "balance_sheet": None, "income": None, "cashflow": None}


def test_real_statement_page_is_picked():
    page = _page("合并资产负债表2025年12月31日本集团本行2025年2024年", 30)
    assert detect_statement_pages([page])["balance_sheet"] == 1


def test_statement_pages_688551(pdf_data):
    assert detect_statement_pages(pdf_data["pages"]) == EXPECTED_688551


def test_statement_pages_159(pdf_159):
    assert detect_statement_pages(pdf_159["pages"]) == EXPECTED_159


def test_three_statements_not_empty(pdf_data):
    for key in ("balance_sheet", "income", "cashflow"):
        assert pdf_data[key] is not None
        assert len(pdf_data[key]) > 5
    assert pdf_data["meta"]["company"]
    assert pdf_data["meta"]["year"] == "2025"
