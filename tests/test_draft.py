"""Day 8/9：投研底稿汇编与双导出。"""
from core.draft import CHAPTERS, assemble, to_docx, to_markdown, to_pdf


def test_assemble_has_all_chapters(pdf_data, index):
    draft = assemble(pdf_data, index=index, use_llm=False)
    for key in CHAPTERS:
        assert key in draft
    assert draft["公司概览"]["公司"]
    assert draft["财务体检"]["比率"].shape[0] == 36
    assert isinstance(draft["结论"], list) and draft["结论"]
    assert isinstance(draft["跟踪清单"], list)


def test_conclusion_numbers_are_traceable(pdf_data, index):
    """规则结论中的数字必须来自底稿事实（防止编造）。"""
    draft = assemble(pdf_data, index=index, use_llm=False)
    facts = " ".join(draft["结论"])
    assert any(ch.isdigit() for ch in facts)


def test_markdown_contains_chapters(pdf_data, index):
    draft = assemble(pdf_data, index=index, use_llm=False)
    md = to_markdown(draft)
    for key in CHAPTERS:
        assert f"## {key}" in md


def test_docx_export(pdf_data, index):
    draft = assemble(pdf_data, index=index, use_llm=False)
    blob = to_docx(draft)
    assert blob[:2] == b"PK"
    assert len(blob) > 10_000


def test_pdf_export(pdf_data, index):
    draft = assemble(pdf_data, index=index, use_llm=False)
    blob = to_pdf(draft)
    assert blob[:4] == b"%PDF"
    assert len(blob) > 3_000
