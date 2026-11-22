"""PDF 提取与三表定位（底层）。

提供：extract_pages（逐页词条 + bbox，供 Day3 溯源复用）、
detect_statement_pages（定位三张合并报表起始页）。
"""
import os
import re

import pdfplumber


class ParseError(Exception):
    """财报 PDF 解析异常（面向用户的中文提示）。"""


def _validate(path: str) -> None:
    if not path or not os.path.exists(path):
        raise ParseError("文件不存在，请重新上传。")
    if not str(path).lower().endswith(".pdf"):
        raise ParseError("仅支持 PDF 文件，请上传 .pdf 格式的年报。")
    if os.path.getsize(path) == 0:
        raise ParseError("PDF 文件为空，请更换文件。")
    # 扩展名可被伪造（上传链路统一落盘为 .pdf），需按文件头确认确为 PDF；
    # 规范允许 %PDF- 前有少量前置字节，故在前 1KB 内查找。
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if b"%PDF-" not in head:
        raise ParseError("文件内容不是有效的 PDF（缺少 %PDF- 文件头），请确认文件类型后重新上传。")


def extract_pages(path: str) -> list:
    """逐页提取词条（含坐标），返回 [{page, words, text}]。"""
    _validate(path)
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            words = []
            for w in page.extract_words():
                words.append(
                    {
                        "text": w["text"],
                        "x0": w["x0"],
                        "x1": w["x1"],
                        "top": w["top"],
                        "bottom": w["bottom"],
                    }
                )
            text = "".join(w["text"] for w in words)
            pages.append({"page": i + 1, "words": words, "text": text})
    if not pages:
        raise ParseError("PDF 无任何页面，无法解析。")
    total_chars = sum(len(p["text"]) for p in pages)
    if total_chars < 100:
        raise ParseError("该 PDF 疑似扫描件（无文字层），无法解析。请提供电子版年报（有文字层）。")
    return pages


# 三表标题：兼容「合并资产负债表」「合并及母公司资产负债表」「合并及公司资产负债表」
# 「母公司资产负债表」「资产负债表」等写法。银行/保险/证券等金融企业报表标题一致。
_TITLE_PREFIX = r"(?<!的)(?:合并及母公司|合并及公司|合并|母公司|公司)?"
_STMT_TITLES = {
    "balance_sheet": re.compile(_TITLE_PREFIX + r"资产负债表"),
    "income": re.compile(_TITLE_PREFIX + r"(?:利润表|损益表)"),
    "cashflow": re.compile(_TITLE_PREFIX + r"现金流量表"),
}

# 报表页锚点科目：标题与数值同页但无法取得独立标题词时，用锚点确认该页确为报表页，
# 避免附注正文中「资产负债表日」「现金流量表项目」等引用被误判为报表起始页。
_STMT_ANCHORS = {
    "balance_sheet": ("资产总计", "负债合计", "流动资产合计"),
    "income": ("营业收入", "营业成本", "营业利润"),
    "cashflow": ("经营活动产生的现金流量净额", "经营活动现金流入",
                 "销售商品、提供劳务收到的现金"),
}

# 表头/行项目提示词：任何一张财务报表首页都会出现其中之一
_ROW_HINTS = ("项目", "期末余额", "年末余额", "年初余额", "本期金额",
              "上期金额", "本期发生额", "本年金额")
# 数字词数量下限：报表首页至少含 3 个数值词；目录页、审计报告页与附注正文多为纯文字
_MIN_NUMS = 3
# 银行/保险等报表表头写作「本集团 本行 2025年 2024年」，不含「项目」；此类页按数值密度识别
_DENSE_NUMS = 8
_NUM_WORD = re.compile(r"^[-－]?(?=[\d,]*\d)[\d,]+(?:\.\d+)?$")
# 目录页特征：目录/页次/页码等字样，或连续点线（…… / .... / ····）
_TOC_KWS = ("目录", "页次", "页码", "CONTENTS", "Contents")
_DOT_LEADER = re.compile(r"(?:…{2,}|\.{4,}|·{4,})")
# 目录页的「标题 + 页码」写法：标题后紧跟页码（排除「2025年12月31日」这类日期）
_PAGE_REF = re.compile(r"^\d{1,4}(?![0-9年月日、，,])")


def _num_count(page: dict) -> int:
    return sum(1 for w in page["words"] if _NUM_WORD.match(w["text"]))


def _is_toc(page: dict) -> bool:
    """目录页：含「目录/页次/页码」等字样，或出现 2 处以上连续点线。"""
    text = page["text"]
    if any(k in text for k in _TOC_KWS):
        return True
    return len(_DOT_LEADER.findall(text)) >= 2


def _is_statement_page(page: dict) -> bool:
    """是否为财务报表首屏：排除目录页，且数值足够。

    常规报表首页含「项目/期末余额」等表头词；银行等金融企业报表首页无此表头，
    改按数值密度（≥8 个数值词）识别。
    """
    if _is_toc(page):
        return False
    n = _num_count(page)
    if n < _MIN_NUMS:
        return False
    if any(h in page["text"] for h in _ROW_HINTS):
        return True
    return n >= _DENSE_NUMS


# 报表标题词前的序号前缀（如「1、」「（一）」「一、」）与「（续）」后缀
_TITLE_STRIP = re.compile(r"^(?:\d+[、.．]|[（(][一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十]+、)")
_TITLE_TAIL = re.compile(r"[（(]续[）)]$")


def _title_hits(page: dict, pattern) -> list:
    """页内「独立成词」的报表标题，返回 [(词, 去序号后的标题文本)]。

    只在词级别做整词匹配，避免把正文句子里的「…日的合并及母公司资产负债表，2025…」
    或「资产负债表日」这类片段误判为报表标题页。
    """
    hits = []
    for w in page["words"]:
        t = _TITLE_TAIL.sub("", _TITLE_STRIP.sub("", w["text"].strip()))
        if len(t) <= 16 and pattern.fullmatch(t):
            hits.append((w, t))
    return hits


def _pick(pattern, pages: list, cursor: int, accept, anchors=None) -> int:
    """挑选报表起始页，优先含「合并」的页，其次母公司页。

    优先按「独立标题词」识别；标题位于页尾、表体在下一页时也接受该标题页。
    无法取得独立标题词时（个别 PDF 标题与数值同词），回退到文本匹配，并用
    anchors 中的锚点科目确认页面确为报表页，避免附注中的引用误判。
    """
    plain = None
    for i, p in enumerate(pages):
        if p["page"] <= cursor or _is_toc(p):
            continue
        hits = _title_hits(p, pattern)
        if not hits:
            if not accept(p):
                continue
            text = p["text"]
            for m in pattern.finditer(text):
                if _PAGE_REF.match(text[m.end():]):
                    continue  # 目录中的页码引用，不是报表页
                # 锚点仅用于排除正文/附注中的中途引用；标题位于页首时视为真实标题页
                if anchors and m.start() > 12 and not any(a in text for a in anchors):
                    continue
                if "合并" in m.group(0):
                    return p["page"]
                if plain is None:
                    plain = p["page"]
                break
            continue
        # 标题本页无数字时，要求下一页确有数值（表体延续到下一页）
        nxt = pages[i + 1] if i + 1 < len(pages) else None
        if _num_count(p) < _MIN_NUMS and (nxt is None or _num_count(nxt) < _DENSE_NUMS):
            continue
        if any("合并" in t for _, t in hits):
            return p["page"]
        if plain is None:
            plain = p["page"]
    return plain


def detect_statement_pages(pages: list) -> dict:
    """定位三张合并报表的起始页（1 起）。找不到对应页则值为 None。

    判定条件：页内含报表标题（合并 / 合并及母公司 / 母公司 等写法）+ 数值。
    目录页（含「目录/页次/页码」、连续点线，或标题后紧跟页码）一律跳过，
    「的合并资产负债表」这类引用也不计入。
    优先选用含「合并」的报表页；仅当全本无合并报表时才回退到母公司报表。
    """
    result = {}
    cursor = 0
    for stmt in ("balance_sheet", "income", "cashflow"):
        picked = _pick(_STMT_TITLES[stmt], pages, cursor, _is_statement_page,
                       _STMT_ANCHORS[stmt])
        result[stmt] = picked
        if picked:
            cursor = picked
    return result
