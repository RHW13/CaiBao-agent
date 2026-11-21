"""PDF 数字索引：把解析出的三表科目与页码、坐标、原文行关联，支撑指标溯源。

数据来源为 statements.load_pdf 的 traces（与计算所用数值严格同源），
另建「三表页全量行」索引用于模糊匹配兜底。
"""
from dataclasses import dataclass, field

from .statements import _NUM_RE, _parse_num, cluster_rows


@dataclass
class Line:
    """PDF 中的一行文字（含数值）。"""
    page: int
    text: str
    nums: list = field(default_factory=list)
    x0: float = 0.0
    x1: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    stmt: str = ""


@dataclass
class PdfIndex:
    pages: list
    entries: list          # 解析成功科目的证据条目
    by_account: dict       # 规范科目名 -> [条目]
    lines: list            # 三表页全量行（用于模糊匹配）
    by_page: dict          # 页码 -> 按 y 排序的行

    def find(self, account: str) -> list:
        return self.by_account.get(account, [])

    def context(self, page: int, top: float, bottom: float, before: int = 1, after: int = 1) -> str:
        """取目标行上下若干行的原文片段。"""
        rows = self.by_page.get(page, [])
        if not rows:
            return ""
        idx = None
        for i, r in enumerate(rows):
            if abs(r.top - top) <= 3.0:
                idx = i
                break
        if idx is None:
            return ""
        lo = max(0, idx - before)
        hi = min(len(rows), idx + after + 1)
        return "\n".join(self._render(r) for r in rows[lo:hi])

    @staticmethod
    def _render(line: Line) -> str:
        if line.nums:
            return f"{line.text}　" + "　".join(f"{n:,.2f}" for n in line.nums)
        return line.text


def _stmt_range(stmt_starts: dict, traces: dict, pages: list) -> dict:
    """计算每张报表的页范围 [起始页, 结束页]。"""
    ranges = {}
    for stmt, start in stmt_starts.items():
        if not start:
            continue
        tr = traces.get(stmt) or []
        end = max((max(t.get("pages") or [t["page"]]) for t in tr), default=start)
        ranges[stmt] = (start, end)
    return ranges


def build_index(pdf_data: dict) -> PdfIndex:
    """由 load_pdf 的返回结果构建索引（不重复解析 PDF）。"""
    pages = pdf_data.get("pages") or []
    traces = pdf_data.get("traces") or {}
    stmt_starts = pdf_data.get("stmt_starts") or {}

    entries = []
    by_account = {}
    for stmt, items in traces.items():
        for t in items:
            entry = {
                "account": t["account"],
                "stmt": stmt,
                "page": t["page"],
                "pages": list(t.get("pages") or [t["page"]]),
                "text": t.get("text", ""),
                "values": t.get("values") or [],
                "x0": t.get("x0", 0.0),
                "x1": t.get("x1", 0.0),
                "top": t.get("top", 0.0),
                "bottom": t.get("bottom", 0.0),
            }
            entries.append(entry)
            by_account.setdefault(entry["account"], []).append(entry)

    ranges = _stmt_range(stmt_starts, traces, pages)
    page_stmt = {}
    for stmt, (lo, hi) in ranges.items():
        for pno in range(lo, hi + 1):
            page_stmt.setdefault(pno, stmt)

    lines = []
    by_page = {}
    for pg in pages:
        pno = pg["page"]
        stmt_of_page = page_stmt.get(pno)
        if not stmt_of_page:
            continue  # 只索引三表所在页，避免全本聚类
        rows = []
        for row in cluster_rows(pg["words"]):
            text_words = [w for w in row if not _NUM_RE.match(w["text"])]
            num_words = [w for w in row if _NUM_RE.match(w["text"])]
            text = "".join(w["text"] for w in sorted(text_words, key=lambda w: w["x0"]))
            nums = [_parse_num(w["text"]) for w in sorted(num_words, key=lambda w: w["x0"])]
            if not text and not nums:
                continue
            rows.append(
                Line(
                    page=pno,
                    text=text,
                    nums=nums,
                    x0=min(w["x0"] for w in row),
                    x1=max(w["x1"] for w in row),
                    top=min(w["top"] for w in row),
                    bottom=max(w["bottom"] for w in row),
                    stmt=stmt_of_page,
                )
            )
        by_page[pno] = rows
        lines.extend(rows)

    return PdfIndex(pages=pages, entries=entries, by_account=by_account, lines=lines, by_page=by_page)
